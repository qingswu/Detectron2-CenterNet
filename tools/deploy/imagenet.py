import argparse
import os
import time
import types
from datetime import datetime

import torch
import torch.onnx
import torch.utils.data
import torchvision.datasets as datasets
import torchvision.models
import torchvision.transforms as transforms
from detectron2.export.meta_modeling import MetaModel, trace_context
from detectron2.utils.logger import setup_logger


# ##### general helper module ########################################

class AverageMeter:
    """
    Computes and stores the average and current value
    """

    def __init__(self, name, fmt=":f"):
        self.name = name
        self.fmt = fmt

        # initialize meter
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / float(self.count)

    @property
    def err(self):
        return 100 - float(self.avg)

    def __str__(self):
        fmt_str = "{name} {val" + self.fmt + "} ({avg" + self.fmt + "})"
        return fmt_str.format(**self.__dict__)


class ProgressMeter(object):

    def __init__(self, num_batches, meters, prefix=""):
        self.batch_fmt_str = self._get_batch_fmt_str(num_batches)
        self.meters = meters
        self.prefix = prefix

    def display(self, batch):
        entries = [self.prefix + self.batch_fmt_str.format(batch)]
        entries += [str(meter) for meter in self.meters]
        print(datetime.now(), "\t".join(entries), flush=True)

    @staticmethod
    def _get_batch_fmt_str(num_batches):
        num_digits = len(str(num_batches // 1))
        fmt = "{:" + str(num_digits) + "d}"
        return "[" + fmt + "/" + fmt.format(num_batches) + "]"


def validate(val_loader, model, cuda=True, print_freq=20):
    batch_time = AverageMeter("Time", ":6.3f")
    top1 = AverageMeter("Acc@1", ":6.2f")
    top5 = AverageMeter("Acc@5", ":6.2f")
    progress = ProgressMeter(
        len(val_loader),
        [batch_time, top1, top5])

    # switch to evaluate mode
    model.eval()

    with torch.no_grad():
        end = time.time()
        for i, data in enumerate(val_loader):
            if cuda:
                data = [t.cuda() for t in data]
            images, target = data
            # compute output
            # notice that this is slightly different from original torch model
            output = model(data)
            # measure and record accuracy
            acc1, acc5 = accuracy(output, target, topk=(1, 5))
            top1.update(acc1[0], images.size(0))
            top5.update(acc5[0], images.size(0))
            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % print_freq == 0:
                progress.display(i)
    print(' * Acc@1 {top1.avg:.3f} Acc@5 {top5.avg:.3f}'
          .format(top1=top1, top5=top5), flush=True)
    print(' * Err@1 {top1.err:.3f} Err@5 {top5.err:.3f}'
          .format(top1=top1, top5=top5), flush=True)


def accuracy(output, target, topk=(1,)):
    """
    Computes the accuracy over the k top predictions for the specified values of k
    """
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res


# ##### core element ########################################


class TorchModel(MetaModel):

    def __init__(self, torch_model):
        cfg = types.SimpleNamespace()
        super(TorchModel, self).__init__(cfg, torch_model)

    def convert_inputs(self, data):
        images, target = data
        return images.cuda()

    def convert_outputs(self, batched_inputs, inputs, results):
        # no postprocessing step is needed for pytorch model
        return results

    def inference(self, inputs):
        # the naming is slightly different
        return self._wrapped_model(inputs)

    def get_input_names(self):
        return ["images"]

    def get_output_names(self):
        return ["prob"]


def get_data_loader(val_dir, batch_size, workers=2):
    val_dataset = datasets.ImageFolder(
        val_dir,
        transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225]),
        ]))
    val_sampler = None
    val_loader = torch.utils.data.DataLoader(
        val_dataset, batch_size=batch_size, shuffle=False,
        num_workers=workers, pin_memory=True, sampler=val_sampler)
    return val_loader


def main():
    parser = argparse.ArgumentParser(description="ImageNet inference example")
    parser.add_argument("data", metavar="DIR", help="path to dataset")
    parser.add_argument("-j", "--workers", default=2, type=int, metavar="N",
                        help="number of data loading workers (default: 2)")
    parser.add_argument("-b", "--batch-size", default=32, type=int,
                        metavar="N",
                        help="mini-batch size (default: 32), this is the total "
                             "batch size of all GPUs on the current node when "
                             "using Data Parallel or Distributed Data Parallel")
    parser.add_argument("--output", default="./output", help="output directory for the converted model")
    parser.add_argument(
        "--format",
        choices=["torch", "onnx", "tensorrt"],
        help="output format",
        default="torch",
    )
    args = parser.parse_args()

    # setup detectron2 logger
    setup_logger()

    if args.output:
        os.makedirs(args.output, exist_ok=True)
    onnx_f = os.path.join(args.output, "model.onnx")
    engine_f = os.path.join(args.output, "model.trt")
    cache_f = os.path.join(args.output, "cache.txt")

    # get data loader
    data_loader = get_data_loader(args.data, args.batch_size, args.workers)

    if args.format == "torch" or args.format == "onnx":
        torch_model = torchvision.models.resnet18(pretrained=True)
        torch_model.cuda()
        model = TorchModel(torch_model)
        if args.format == "onnx":
            data = next(iter(data_loader))
            inputs = model.convert_inputs(data)
            with trace_context(model):
                torch.onnx.export(model, (inputs,), onnx_f, verbose=True, input_names=model.get_input_names(),
                                  output_names=model.get_output_names())
                return

    # validation
    validate(data_loader, model)


if __name__ == "__main__":
    main()
