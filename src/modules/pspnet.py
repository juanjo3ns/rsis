import torch
import torch.nn as nn
import torchvision
import resnet


class ModelBuilder():
    # custom weights initialization
    def weights_init(self, m):
        classname = m.__class__.__name__
        if classname.find('Conv') != -1:
            m.weight.data.normal_(0.0, 0.001)
        elif classname.find('BatchNorm') != -1:
            m.weight.data.normal_(1.0, 0.02)
            m.bias.data.fill_(0)
        elif classname.find('Linear') != -1:
            m.weight.data.normal_(0.0, 0.0001)

    def build_encoder(self, arch='resnet34_dilated8', fc_dim=512, weights=''):
        pretrained = True if len(weights) == 0 else False
        if arch == 'vgg16_dilated8':
            orig_vgg = torchvision.models.vgg16(pretrained=pretrained)
            conv5 = (24, 26, 28)
            pool4n5 = (23, 30)
            net_encoder = VggDilated(orig_vgg,
                                     conv5,
                                     pool4n5,
                                     dropout2d=True)
        elif arch == 'vgg19_dilated8':
            orig_vgg = torchvision.models.vgg19(pretrained=pretrained)
            conv5 = (28, 30, 32, 34)
            pool4n5 = (27, 36)
            net_encoder = VggDilated(orig_vgg,
                                     conv5,
                                     pool4n5,
                                     dropout2d=True)
        elif arch == 'resnet34':
            orig_resnet = resnet.__dict__['resnet34'](pretrained=pretrained)
            net_encoder = Resnet(orig_resnet)
        elif arch == 'resnet34_dilated8':
            orig_resnet = resnet.__dict__['resnet34'](pretrained=pretrained)
            net_encoder = ResnetDilated(orig_resnet,
                                        dilate_scale=8)
        elif arch == 'resnet34_dilated16':
            orig_resnet = resnet.__dict__['resnet34'](pretrained=pretrained)
            net_encoder = ResnetDilated(orig_resnet,
                                        dilate_scale=16)
        elif arch == 'resnet50':
            orig_resnet = resnet.__dict__['resnet50'](pretrained=pretrained)
            net_encoder = Resnet(orig_resnet)
        elif arch == 'resnet50_dilated8':
            orig_resnet = resnet.__dict__['resnet50'](pretrained=pretrained)
            net_encoder = ResnetDilated(orig_resnet,
                                        dilate_scale=8)
        elif arch == 'resnet50_dilated16':
            orig_resnet = resnet.__dict__['resnet50'](pretrained=pretrained)
            net_encoder = ResnetDilated(orig_resnet,
                                        dilate_scale=16)
        else:
            raise Exception('Architecture undefined!')

        # net_encoder.apply(self.weights_init)
        if len(weights) > 0:
            print('Loading weights for net_encoder')
            net_encoder.load_state_dict(
                torch.load(weights, map_location=lambda storage, loc: storage))
        return net_encoder

    def build_decoder(self, fc_dim=512, num_class=150,
                      segSize=384, weights=''):

        net_decoder = PSPBilinear(num_class=num_class,
                                fc_dim=fc_dim,
                                segSize=segSize)


        net_decoder.apply(self.weights_init)
        if len(weights) > 0:
            print('Loading weights for net_decoder')
            net_decoder.load_state_dict(
                torch.load(weights, map_location=lambda storage, loc: storage))
        return net_decoder


class VggDilated(nn.Module):
    def __init__(self, orig_vgg, conv5, pool4n5, dropout2d=True):
        super(VggDilated, self).__init__()

        # make conv5 dilated
        for i in conv5:
            orig_vgg.features[i].dilation = (2, 2)
            orig_vgg.features[i].padding = (2, 2)
        # take away pool4 and pool5
        modules = [x for i, x in enumerate(orig_vgg.features)
                   if i not in pool4n5]
        self.features = nn.Sequential(*(modules))

        # convert fc weights into conv1x1 weights
        self.conv6 = nn.Conv2d(512, 4096, 7, 1, 12, 4)
        self.conv6.weight.data.copy_(
            orig_vgg.classifier[0].weight.data.resize_(4096, 512, 7, 7))
        self.conv6.bias.data.copy_(orig_vgg.classifier[0].bias.data)
        self.conv7 = nn.Conv2d(4096, 4096, 1, 1, 0)
        self.conv7.weight.data.copy_(
            orig_vgg.classifier[3].weight.data.resize_(4096, 4096, 1, 1))
        self.conv7.bias.data.copy_(orig_vgg.classifier[3].bias.data)

        self.relu6 = nn.ReLU(True)
        self.relu7 = nn.ReLU(True)

        if dropout2d:
            self.dropout6 = nn.Dropout2d(0.5)
            self.dropout7 = nn.Dropout2d(0.5)
        else:
            self.dropout6 = nn.Dropout(0.5)
            self.dropout7 = nn.Dropout(0.5)

    def forward(self, x):
        x = self.features(x)
        x = self.dropout6(self.relu6(self.conv6(x)))
        x = self.dropout7(self.relu7(self.conv7(x)))

        return x


class Resnet(nn.Module):
    def __init__(self, orig_resnet):
        super(Resnet, self).__init__()

        # take pretrained resnet, except AvgPool and FC
        self.conv1 = orig_resnet.conv1
        self.bn1 = orig_resnet.bn1
        self.relu1 = orig_resnet.relu1
        self.conv2 = orig_resnet.conv2
        self.bn2 = orig_resnet.bn2
        self.relu2 = orig_resnet.relu2
        self.conv3 = orig_resnet.conv3
        self.bn3 = orig_resnet.bn3
        self.relu3 = orig_resnet.relu3
        self.maxpool = orig_resnet.maxpool
        self.layer1 = orig_resnet.layer1
        self.layer2 = orig_resnet.layer2
        self.layer3 = orig_resnet.layer3
        self.layer4 = orig_resnet.layer4

    def forward(self, x):
        x = self.relu1(self.bn1(self.conv1(x)))
        x = self.relu2(self.bn2(self.conv2(x)))
        x = self.relu3(self.bn3(self.conv3(x)))
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        return x


class ResnetDilated(nn.Module):
    def __init__(self, orig_resnet, dilate_scale=8, dropout2d=False):
        super(ResnetDilated, self).__init__()
        self.dropout2d = dropout2d
        from functools import partial

        if dilate_scale == 8:
            orig_resnet.layer3.apply(
                partial(self._nostride_dilate, dilate=2))
            orig_resnet.layer4.apply(
                partial(self._nostride_dilate, dilate=4))
        elif dilate_scale == 16:
            orig_resnet.layer4.apply(
                partial(self._nostride_dilate, dilate=2))

        # take pretrained resnet, except AvgPool and FC
        self.conv1 = orig_resnet.conv1
        self.bn1 = orig_resnet.bn1
        self.relu1 = orig_resnet.relu1
        self.conv2 = orig_resnet.conv2
        self.bn2 = orig_resnet.bn2
        self.relu2 = orig_resnet.relu2
        self.conv3 = orig_resnet.conv3
        self.bn3 = orig_resnet.bn3
        self.relu3 = orig_resnet.relu3
        self.maxpool = orig_resnet.maxpool
        self.layer1 = orig_resnet.layer1
        self.layer2 = orig_resnet.layer2
        self.layer3 = orig_resnet.layer3
        self.layer4 = orig_resnet.layer4

        if self.dropout2d:
            self.dropout = nn.Dropout2d(0.5)

    def _nostride_dilate(self, m, dilate):
        classname = m.__class__.__name__
        if classname.find('Conv') != -1:
            # the convolution with stride
            if m.stride == (2, 2):
                m.stride = (1, 1)
                if m.kernel_size == (3, 3):
                    m.dilation = (dilate//2, dilate//2)
                    m.padding = (dilate//2, dilate//2)
            # other convoluions
            else:
                if m.kernel_size == (3, 3):
                    m.dilation = (dilate, dilate)
                    m.padding = (dilate, dilate)

    def forward(self, x):
        x = self.relu1(self.bn1(self.conv1(x)))
        x = self.relu2(self.bn2(self.conv2(x)))
        x = self.relu3(self.bn3(self.conv3(x)))
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        if self.dropout2d:
            x = self.dropout(x)
        return x


# pyramid pooling, bilinear upsample
class PSPBilinear(nn.Module):
    def __init__(self, num_class=150, fc_dim=4096, segSize=384,
                 pool_scales=(1, 2, 3, 6)):
        super(PSPBilinear, self).__init__()
        self.segSize = segSize

        self.psp = []
        for scale in pool_scales:
            self.psp.append(nn.Sequential(
                nn.AdaptiveAvgPool2d(scale),
                nn.Conv2d(fc_dim, 512, kernel_size=1, bias=False),
                nn.BatchNorm2d(512),
                nn.ReLU(inplace=True)
            ))
        self.psp = nn.ModuleList(self.psp)
        self.conv_last = nn.Sequential(
            nn.Conv2d(fc_dim+len(pool_scales)*512, 512,
                      kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(512),
            nn.ReLU(inplace=True),
            nn.Dropout(0.1),
            nn.Conv2d(512, num_class, kernel_size=1)
        )

    def forward(self, x, segSize=None):
        if segSize is None:
            segSize = (self.segSize, self.segSize)
        elif isinstance(segSize, int):
            segSize = (segSize, segSize)

        input_size = x.size()
        psp_out = [x]
        for pool_scale in self.psp:
            psp_out.append(nn.functional.upsample(
                pool_scale(x),
                (input_size[2], input_size[3]),
                mode='bilinear'))
        x = torch.cat(psp_out, 1)

        '''
        #remove last layer from forward pass, to be replaced by new one
        x = self.conv_last(x)
        if not (input_size[2] == segSize[0] and input_size[3] == segSize[1]):
            x = nn.functional.upsample(x, size=segSize, mode='bilinear')
        '''
        return x


class PSPNet_R50_Dilated8(nn.Module):
    def __init__(self,weights_encoder, weights_decoder):
        super(PSPNet_R50_Dilated8,self).__init__()
        builder = ModelBuilder()
        self.encoder = builder.build_encoder(arch='resnet50_dilated8',
                                            fc_dim=2048,
                                            weights=weights_encoder)
        self.decoder = builder.build_decoder(fc_dim=2048,
                                            segSize=None,
                                            weights=weights_decoder)

    def forward(self, img):
        x = self.encoder(img)
        x = self.decoder(x, segSize=(img.size(2), img.size(3)))
        return x