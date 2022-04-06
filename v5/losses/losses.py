import torch
import torch.nn as nn
import math
import v5_utils

def gradient(inputs, outputs):
    d_points = torch.ones_like(outputs, requires_grad=False, device=outputs.device)
    points_grad = torch.autograd.grad(
        outputs=outputs,
        inputs=inputs,
        grad_outputs=d_points,
        create_graph=True,
        retain_graph=True)
    return points_grad

class ODFLoss(nn.Module):
    def __init__(self):
        super().__init__()
        self.mse_loss = nn.MSELoss(reduction="none")

    def forward(self, output, target):
        assert isinstance(output, list)
        B = len(target)
        losses = []
        for b in range(B):
            losses.append(self.mse_loss(output[b], target[b]))
        return torch.mean(torch.cat(losses))

class MaskLoss(nn.Module):
    def __init__(self):
        super().__init__()
        self.bce_loss = nn.BCELoss(reduction="none")

    def forward(self, output, target):
        assert isinstance(output, list)
        B = len(target)
        losses = []
        for b in range(B):
            GTMask, _ = target[b]
            # print("One instance")
            # print(output[b][:10])
            # print(self.bce_loss(output[b][:10], target[b][:10]))
            losses.append(self.bce_loss(output[b], GTMask))
        return torch.mean(torch.cat(losses))

class MaskLossV2(nn.Module):
    def __init__(self):
        super().__init__()
        
    def forward(self, output, target):
        assert isinstance(output, list)
        B = len(target)
        losses = []
        for b in range(B):
            losses.append(nn.functional.binary_cross_entropy_with_logits(output[b], target[b], reduction='none'))
        return torch.mean(torch.cat(losses))


class DepthLoss(nn.Module):
    def __init__(self, Thresh=v5_utils.INTERSECTION_MASK_THRESHOLD):
        super().__init__()
        self.Sigmoid = nn.Sigmoid()
        self.Thresh = Thresh

    def forward(self, output, target):
        return self.computeLoss(output, target)

    def computeLoss(self, output, target):
        assert isinstance(output, list) # For custom collate
        B = len(target) # Number of batches with custom collate
        Loss = torch.tensor(0.).to(target[0][0].device)
        for b in range(B):
            # Single batch version
            GTMask, GTDepth = target[b]

            PredDepth = output[b]



            ValidRaysIdx = GTMask.to(torch.bool)  # Use ground truth mask
            Loss += self.L2(GTDepth[ValidRaysIdx], PredDepth[ValidRaysIdx])
        Loss /= B

        return Loss

    def L2(self, labels, predictions):
        Loss = torch.mean(torch.square(labels - predictions))
        if math.isnan(Loss) or math.isinf(Loss):
            return torch.tensor(0)
        return Loss

class IntersectionLoss(nn.Module):
    def __init__(self, Thresh=v5_utils.INTERSECTION_MASK_THRESHOLD):
        super().__init__()
        self.Sigmoid = nn.Sigmoid()
        self.Thresh = Thresh
        self.BCE = nn.BCELoss(reduction='mean')


    def forward(self, output, target):
        return self.computeLoss(output, target)

    def computeLoss(self, output, target):
        assert isinstance(output, list) # For custom collate
        B = len(target) # Number of batches with custom collate
        Loss = 0
        for b in range(B):
            # Single batch version
            GTMask, _ = target[b]

            if len(output[b]) == 2:
                PredMaskConf, _ = output[b]
            else:
                PredMaskConf, _, _, _ = output[b]

            PredMaskConfSig = self.Sigmoid(PredMaskConf)
            PredMaskMaxConfVal = PredMaskConfSig

            Loss += 1.0 * self.BCE(PredMaskMaxConfVal.to(torch.float), GTMask.to(torch.float))

        Loss /= B

        return Loss


class DepthFieldRegularizingLoss(nn.Module):

    def __init__(self, Thresh=v5_utils.INTERSECTION_MASK_THRESHOLD):
        super().__init__()
        self.Sigmoid = nn.Sigmoid()
        self.Thresh = Thresh

        
    def forward(self, model, data):
        return self.computeLoss(model, data)
    
    def computeLoss(self, model, data):

        assert isinstance(data, list) # For custom collate
        B = len(data) # Number of batches with custom collate
        Loss = 0

        for b in range(B):
            # Single batch version
            TrainCoords = data[b]
            OtherCoords = torch.tensor(v5_utils.odf_domain_sampler(TrainCoords.shape[0]), dtype=torch.float32).to(TrainCoords.device)
            Coords = torch.cat([TrainCoords, OtherCoords], dim=0)

            Coords.requires_grad_()
            output = model([Coords])[0]
            if len(output) == 2:
                PredMaskConf, PredDepth = output
            else:
                PredMaskConf, PredDepth, _, _ = output
            PredMaskConfSig = self.Sigmoid(PredMaskConf)
            intersections = PredMaskConfSig.squeeze()
            depths = PredDepth

            x_grads = gradient(Coords, depths)[0][...,:3]

            odf_gradient_directions = Coords[:,3:]


            if torch.sum(intersections > self.Thresh) != 0.:
                grad_dir_loss = torch.mean(torch.abs(torch.sum(odf_gradient_directions[intersections>self.Thresh]*x_grads[intersections>self.Thresh], dim=-1) + 1.))
            else:
                grad_dir_loss = torch.tensor(0.).to(TrainCoords.device)

            Loss += 1.0 * grad_dir_loss

        Loss /= B

        return Loss


class ConstantRegularizingLoss(nn.Module):

    def __init__(self, Thresh=v5_utils.INTERSECTION_MASK_THRESHOLD):
        super().__init__()
        self.Sigmoid = nn.Sigmoid()
        self.Thresh = Thresh
        
    def forward(self, model, data):
        return self.computeLoss(model, data)
    
    def computeLoss(self, model, data):
        assert isinstance(data, list) # For custom collate
        B = len(data) # Number of batches with custom collate
        Loss = 0

        for b in range(B):
            # Single batch version
            TrainCoords = data[b]
            OtherCoords = torch.tensor(v5_utils.odf_domain_sampler(TrainCoords.shape[0]), dtype=torch.float32).to(TrainCoords.device)
            Coords = torch.cat([TrainCoords, OtherCoords], dim=0)

            Coords.requires_grad_()
            output = model([Coords])[0]
            assert(len(output) > 2)
            _, _, PredMaskConst, PredConst = output
            PredMaskConstSig = self.Sigmoid(PredMaskConst)
            constant_mask = PredMaskConstSig.squeeze()
            constant = PredConst

            x_grads = gradient(Coords, constant)[0][...,:3]

            odf_gradient_directions = Coords[:,3:]


            if torch.sum(constant_mask > self.Thresh) != 0.:
                grad_dir_loss = torch.mean(torch.abs(torch.sum(odf_gradient_directions[constant_mask>self.Thresh]*x_grads[constant_mask>self.Thresh], dim=-1)))
            else:
                grad_dir_loss = torch.tensor(0.).to(TrainCoords.device)

            Loss += 1.0 * grad_dir_loss

        Loss /= B

        return Loss
