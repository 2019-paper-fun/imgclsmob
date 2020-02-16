"""
    SimplePose for COCO Keypoint, implemented in PyTorch.
    Original paper: 'Simple Baselines for Human Pose Estimation and Tracking,' https://arxiv.org/abs/1804.06208.
"""

__all__ = ['SimplePose', 'simplepose_resnet18_coco', 'simplepose_resnet50b_coco', 'simplepose_resnet101b_coco',
           'simplepose_resnet152b_coco', 'simplepose_resneta50b_coco', 'simplepose_resneta101b_coco',
           'simplepose_resneta152b_coco', 'HeatmapMaxDetBlock']

import os
import torch
import torch.nn as nn
from .common import get_activation_layer, conv1x1
from .resnet import resnet18, resnet50b, resnet101b, resnet152b
from .resneta import resneta50b, resneta101b, resneta152b


class DeconvBlock(nn.Module):
    """
    Deconvolution block with batch normalization and activation.

    Parameters:
    ----------
    in_channels : int
        Number of input channels.
    out_channels : int
        Number of output channels.
    kernel_size : int or tuple/list of 2 int
        Convolution window size.
    stride : int or tuple/list of 2 int
        Strides of the deconvolution.
    padding : int or tuple/list of 2 int
        Padding value for deconvolution layer.
    out_padding : int or tuple/list of 2 int
        Output padding value for deconvolution layer.
    dilation : int or tuple/list of 2 int, default 1
        Dilation value for deconvolution layer.
    groups : int, default 1
        Number of groups.
    bias : bool, default False
        Whether the layer uses a bias vector.
    use_bn : bool, default True
        Whether to use BatchNorm layer.
    bn_eps : float, default 1e-5
        Small float added to variance in Batch norm.
    activation : function or str or None, default nn.ReLU(inplace=True)
        Activation function or name of activation function.
    """
    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride,
                 padding,
                 out_padding=0,
                 dilation=1,
                 groups=1,
                 bias=False,
                 use_bn=True,
                 bn_eps=1e-5,
                 activation=(lambda: nn.ReLU(inplace=True))):
        super(DeconvBlock, self).__init__()
        self.activate = (activation is not None)
        self.use_bn = use_bn

        self.conv = nn.ConvTranspose2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            output_padding=out_padding,
            dilation=dilation,
            groups=groups,
            bias=bias)
        if self.use_bn:
            self.bn = nn.BatchNorm2d(
                num_features=out_channels,
                eps=bn_eps)
        if self.activate:
            self.activ = get_activation_layer(activation)

    def forward(self, x):
        x = self.conv(x)
        if self.use_bn:
            x = self.bn(x)
        if self.activate:
            x = self.activ(x)
        return x


class HeatmapMaxDetBlock(nn.Module):
    """
    Heatmap maximum detector block.
    """
    def __init__(self):
        super(HeatmapMaxDetBlock, self).__init__()

    def forward(self, x):
        heatmap = x
        vector_dim = 2
        batch = heatmap.shape[0]
        channels = heatmap.shape[1]
        in_size = x.shape[2:]
        heatmap_vector = heatmap.view(batch, channels, -1)
        scores, indices = heatmap_vector.max(dim=vector_dim, keepdims=True)
        scores_mask = (scores > 0.0).float()
        pts_x = (indices % in_size[1]) * scores_mask
        pts_y = (indices // in_size[1]) * scores_mask
        pts = torch.cat((pts_x, pts_y, scores), dim=vector_dim)
        for b in range(batch):
            for k in range(channels):
                hm = heatmap[b, k, :, :]
                px = int(pts[b, k, 0])
                py = int(pts[b, k, 1])
                if (0 < px < in_size[1] - 1) and (0 < py < in_size[0] - 1):
                    pts[b, k, 0] += (hm[py, px + 1] - hm[py, px - 1]).sign() * 0.25
                    pts[b, k, 1] += (hm[py + 1, px] - hm[py - 1, px]).sign() * 0.25
        return pts

    @staticmethod
    def calc_flops(x):
        assert (x.shape[0] == 1)
        num_flops = x.numel() + 26 * x.shape[1]
        num_macs = 0
        return num_flops, num_macs


class SimplePose(nn.Module):
    """
    SimplePose model from 'Simple Baselines for Human Pose Estimation and Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    backbone : nn.Sequential
        Feature extractor.
    backbone_out_channels : int
        Number of output channels for the backbone.
    channels : list of int
        Number of output channels for each decoder unit.
    return_heatmap : bool, default False
        Whether to return only heatmap.
    in_channels : int, default 3
        Number of input channels.
    in_size : tuple of two ints, default (256, 192)
        Spatial size of the expected input image.
    keypoints : int, default 17
        Number of keypoints.
    """
    def __init__(self,
                 backbone,
                 backbone_out_channels,
                 channels,
                 return_heatmap=False,
                 in_channels=3,
                 in_size=(256, 192),
                 keypoints=17):
        super(SimplePose, self).__init__()
        assert (in_channels == 3)
        self.in_size = in_size
        self.keypoints = keypoints
        self.return_heatmap = return_heatmap

        self.backbone = backbone

        self.decoder = nn.Sequential()
        in_channels = backbone_out_channels
        for i, out_channels in enumerate(channels):
            self.decoder.add_module("unit{}".format(i + 1), DeconvBlock(
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=4,
                stride=2,
                padding=1))
            in_channels = out_channels
        self.decoder.add_module("final_block", conv1x1(
            in_channels=in_channels,
            out_channels=keypoints,
            bias=True))

        self.heatmap_max_det = HeatmapMaxDetBlock()

        self._init_params()

    def _init_params(self):
        for name, module in self.named_modules():
            if isinstance(module, nn.Conv2d):
                nn.init.kaiming_uniform_(module.weight)
                if module.bias is not None:
                    nn.init.constant_(module.bias, 0)

    def forward(self, x):
        x = self.backbone(x)
        heatmap = self.decoder(x)
        if self.return_heatmap:
            return heatmap
        else:
            keypoints = self.heatmap_max_det(heatmap)
            return keypoints


def get_simplepose(backbone,
                   backbone_out_channels,
                   keypoints,
                   model_name=None,
                   pretrained=False,
                   root=os.path.join("~", ".torch", "models"),
                   **kwargs):
    """
    Create SimplePose model with specific parameters.

    Parameters:
    ----------
    backbone : nn.Sequential
        Feature extractor.
    backbone_out_channels : int
        Number of output channels for the backbone.
    keypoints : int
        Number of keypoints.
    model_name : str or None, default None
        Model name for loading pretrained model.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.torch/models'
        Location for keeping the model parameters.
    """
    channels = [256, 256, 256]

    net = SimplePose(
        backbone=backbone,
        backbone_out_channels=backbone_out_channels,
        channels=channels,
        keypoints=keypoints,
        **kwargs)

    if pretrained:
        if (model_name is None) or (not model_name):
            raise ValueError("Parameter `model_name` should be properly initialized for loading pretrained model.")
        from .model_store import download_model
        download_model(
            net=net,
            model_name=model_name,
            local_model_store_dir_path=root)

    return net


def simplepose_resnet18_coco(pretrained_backbone=False, keypoints=17, **kwargs):
    """
    SimplePose model on the base of ResNet-18 for COCO Keypoint from 'Simple Baselines for Human Pose Estimation and
    Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    pretrained_backbone : bool, default False
        Whether to load the pretrained weights for feature extractor.
    keypoints : int, default 17
        Number of keypoints.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.torch/models'
        Location for keeping the model parameters.
    """
    backbone = resnet18(pretrained=pretrained_backbone).features
    del backbone[-1]
    return get_simplepose(backbone=backbone, backbone_out_channels=512, keypoints=keypoints,
                          model_name="simplepose_resnet18_coco", **kwargs)


def simplepose_resnet50b_coco(pretrained_backbone=False, keypoints=17, **kwargs):
    """
    SimplePose model on the base of ResNet-50b for COCO Keypoint from 'Simple Baselines for Human Pose Estimation and
    Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    pretrained_backbone : bool, default False
        Whether to load the pretrained weights for feature extractor.
    keypoints : int, default 17
        Number of keypoints.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.torch/models'
        Location for keeping the model parameters.
    """
    backbone = resnet50b(pretrained=pretrained_backbone).features
    del backbone[-1]
    return get_simplepose(backbone=backbone, backbone_out_channels=2048, keypoints=keypoints,
                          model_name="simplepose_resnet50b_coco", **kwargs)


def simplepose_resnet101b_coco(pretrained_backbone=False, keypoints=17, **kwargs):
    """
    SimplePose model on the base of ResNet-101b for COCO Keypoint from 'Simple Baselines for Human Pose Estimation
    and Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    pretrained_backbone : bool, default False
        Whether to load the pretrained weights for feature extractor.
    keypoints : int, default 17
        Number of keypoints.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.torch/models'
        Location for keeping the model parameters.
    """
    backbone = resnet101b(pretrained=pretrained_backbone).features
    del backbone[-1]
    return get_simplepose(backbone=backbone, backbone_out_channels=2048, keypoints=keypoints,
                          model_name="simplepose_resnet101b_coco", **kwargs)


def simplepose_resnet152b_coco(pretrained_backbone=False, keypoints=17, **kwargs):
    """
    SimplePose model on the base of ResNet-152b for COCO Keypoint from 'Simple Baselines for Human Pose Estimation
    and Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    pretrained_backbone : bool, default False
        Whether to load the pretrained weights for feature extractor.
    keypoints : int, default 17
        Number of keypoints.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.torch/models'
        Location for keeping the model parameters.
    """
    backbone = resnet152b(pretrained=pretrained_backbone).features
    del backbone[-1]
    return get_simplepose(backbone=backbone, backbone_out_channels=2048, keypoints=keypoints,
                          model_name="simplepose_resnet152b_coco", **kwargs)


def simplepose_resneta50b_coco(pretrained_backbone=False, keypoints=17, **kwargs):
    """
    SimplePose model on the base of ResNet(A)-50b for COCO Keypoint from 'Simple Baselines for Human Pose Estimation
    and Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    pretrained_backbone : bool, default False
        Whether to load the pretrained weights for feature extractor.
    keypoints : int, default 17
        Number of keypoints.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.torch/models'
        Location for keeping the model parameters.
    """
    backbone = resneta50b(pretrained=pretrained_backbone).features
    del backbone[-1]
    return get_simplepose(backbone=backbone, backbone_out_channels=2048, keypoints=keypoints,
                          model_name="simplepose_resneta50b_coco", **kwargs)


def simplepose_resneta101b_coco(pretrained_backbone=False, keypoints=17, **kwargs):
    """
    SimplePose model on the base of ResNet(A)-101b for COCO Keypoint from 'Simple Baselines for Human Pose Estimation
    and Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    pretrained_backbone : bool, default False
        Whether to load the pretrained weights for feature extractor.
    keypoints : int, default 17
        Number of keypoints.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.torch/models'
        Location for keeping the model parameters.
    """
    backbone = resneta101b(pretrained=pretrained_backbone).features
    del backbone[-1]
    return get_simplepose(backbone=backbone, backbone_out_channels=2048, keypoints=keypoints,
                          model_name="simplepose_resneta101b_coco", **kwargs)


def simplepose_resneta152b_coco(pretrained_backbone=False, keypoints=17, **kwargs):
    """
    SimplePose model on the base of ResNet(A)-152b for COCO Keypoint from 'Simple Baselines for Human Pose Estimation
    and Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    pretrained_backbone : bool, default False
        Whether to load the pretrained weights for feature extractor.
    keypoints : int, default 17
        Number of keypoints.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.torch/models'
        Location for keeping the model parameters.
    """
    backbone = resneta152b(pretrained=pretrained_backbone).features
    del backbone[-1]
    return get_simplepose(backbone=backbone, backbone_out_channels=2048, keypoints=keypoints,
                          model_name="simplepose_resneta152b_coco", **kwargs)


def _calc_width(net):
    import numpy as np
    net_params = filter(lambda p: p.requires_grad, net.parameters())
    weight_count = 0
    for param in net_params:
        weight_count += np.prod(param.size())
    return weight_count


def _test():
    in_size = (256, 192)
    keypoints = 17
    return_heatmap = False
    pretrained = False

    models = [
        simplepose_resnet18_coco,
        simplepose_resnet50b_coco,
        simplepose_resnet101b_coco,
        simplepose_resnet152b_coco,
        simplepose_resneta50b_coco,
        simplepose_resneta101b_coco,
        simplepose_resneta152b_coco,
    ]

    for model in models:

        net = model(pretrained=pretrained, in_size=in_size, return_heatmap=return_heatmap)

        # net.train()
        net.eval()
        weight_count = _calc_width(net)
        print("m={}, {}".format(model.__name__, weight_count))
        assert (model != simplepose_resnet18_coco or weight_count == 15376721)
        assert (model != simplepose_resnet50b_coco or weight_count == 33999697)
        assert (model != simplepose_resnet101b_coco or weight_count == 52991825)
        assert (model != simplepose_resnet152b_coco or weight_count == 68635473)
        assert (model != simplepose_resneta50b_coco or weight_count == 34018929)
        assert (model != simplepose_resneta101b_coco or weight_count == 53011057)
        assert (model != simplepose_resneta152b_coco or weight_count == 68654705)

        batch = 14
        x = torch.randn(batch, 3, in_size[0], in_size[1])
        y = net(x)
        assert ((y.shape[0] == batch) and (y.shape[1] == keypoints))
        if return_heatmap:
            assert ((y.shape[2] == x.shape[2] // 4) and (y.shape[3] == x.shape[3] // 4))
        else:
            assert (y.shape[2] == 3)


if __name__ == "__main__":
    _test()
