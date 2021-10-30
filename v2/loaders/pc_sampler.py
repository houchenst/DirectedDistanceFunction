import argparse
import random
import beacon.utils as butils
import trimesh
import torch
import math
from tqdm import tqdm
import multiprocessing as mp
from itertools import repeat
from functools import partial

import tk3dv.nocstools.datastructures as ds
from PyQt5.QtWidgets import QApplication
import PyQt5.QtCore as QtCore
from PyQt5.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
import numpy as np

from tk3dv.pyEasel import *
from EaselModule import EaselModule
from Easel import Easel
import OpenGL.GL as gl
import OpenGL.arrays.vbo as glvbo
from sklearn.neighbors import NearestNeighbors

FileDirPath = os.path.dirname(__file__)
sys.path.append(os.path.join(FileDirPath, '../'))
sys.path.append(os.path.join(FileDirPath, '../../'))
import odf_utils
from odf_dataset import DEFAULT_RADIUS, ODFDatasetVisualizer, ODFDatasetLiveVisualizer

PC_VERT_NOISE = 0.02
PC_TAN_NOISE = 0.02
PC_UNIFORM_RATIO = 100
PC_VERT_RATIO = 0
PC_TAN_RATIO = 0
PC_RADIUS = 1.25
PC_MAX_INTERSECT = 1
PC_SAMPLER_THRESH = 0.01
PC_SAMPLER_NEG_MINOFFSET = 0.05
PC_SAMPLER_NEG_MAXOFFSET = 0.25

class PointCloudSampler():
    def __init__(self, Vertices, VertexNormals, TargetRays):
        self.Vertices = Vertices
        self.VertexNormals = VertexNormals
        self.nTargetRays = TargetRays
        assert self.Vertices.shape[0] == self.VertexNormals.shape[0]
        print('[ INFO ]: Found {} vertices with normals. Will try to sample {} rays in total.'.format(len(self.Vertices), self.nTargetRays))

        self.Coordinates = None
        self.Intersects = None
        self.Depths = None

        self.sample(self.nTargetRays)

    @staticmethod
    def sample_directions_numpy(nDirs, normal=None, ndim=3):
        vec = np.random.randn(nDirs, ndim)
        vec /= np.linalg.norm(vec, axis=0)
        if normal is not None:
            # Select only if direction is in the sae half space as normal
            DotP = np.sum(np.multiply(vec, normal), axis=1)
            ValidIdx = DotP > 0.0
            InvalidIdx = DotP <= 0.0
            # vec = vec[ValidIdx]
            vec[InvalidIdx] = normal # Set invalid to just be normal

        return vec

    @staticmethod
    def sample_directions_prune_normal_numpy(nDirs, vertex, normal, points, thresh):
        Dirs = np.random.randn(nDirs, 3)
        Dirs /= np.linalg.norm(Dirs, axis=0)

        # Select only if direction is in the same half space as normal
        DotP = np.sum(np.multiply(Dirs, normal), axis=1)
        ValidIdx = DotP > 0.0
        Dirs = Dirs[ValidIdx]

        # Next check if chosen directions are within a threshold of vertices
        d = np.linalg.norm(vertex)
        PlaneEq = np.array([[normal[0], normal[1], normal[2], d]])
        HSVal = np.dot(PlaneEq, np.hstack((points, np.ones((len(points), 1)))).T )
        # print(HSVal.shape)
        # print(np.min(HSVal), np.min(HSVal))
        HSIdx = np.squeeze(HSVal) < 0
        # print(np.sum(HSIdx))
        HalfSpacePoints = points[HSIdx]
        # Point-line distance, ray form: https://www.math.kit.edu/ianm2/lehre/am22016s/media/distance-harvard.pdf
        PQ = vertex - HalfSpacePoints
        P2LDistances = np.linalg.norm(np.abs(np.cross(PQ[:, None, :], Dirs[None, :, :])), axis=2)
        FailedIdx = P2LDistances < thresh # Boolean array of all possible vertices and ray intersections that failed the threshold test
        # Find directions that failed the test
        FailedDirIdxSum = np.sum(FailedIdx, axis=0)
        SuccessDirIdx = FailedDirIdxSum==0 # The vertex will be at a distance of 0 so, we look for anything more than 1

        Dirs = Dirs[SuccessDirIdx]

        return Dirs

    @staticmethod
    def sample_directions_prune_numpy(nDirs, vertex, points, thresh):
        Dirs = np.random.randn(nDirs, 3)
        Dirs /= np.linalg.norm(Dirs, axis=0)

        # Next check if chosen directions are within a threshold of vertices
        PQ = vertex - points
        P2LDistances = np.linalg.norm(np.abs(np.cross(PQ[:, None, :], Dirs[None, :, :])), axis=2)
        FailedIdx = P2LDistances < thresh  # Boolean array of all possible vertices and ray intersections that failed the threshold test
        # Find directions that failed the test
        FailedDirIdxSum = np.sum(FailedIdx, axis=0)
        SuccessDirIdx = FailedDirIdxSum <= 1  # The vertex will be at a distance of 0 so, we look for anything more than 1

        Dirs = Dirs[SuccessDirIdx]

        return Dirs

    @staticmethod
    def sample_directions_torch(nDirs, normal=None, device='cpu', ndim=3, dtype=torch.float32):
        # Sample more than needed then prume
        vec = torch.randn((nDirs, ndim), device=device, dtype=dtype)
        vec /= torch.linalg.norm(vec, axis=0)
        if normal is not None:
            # Select only if direction is in the sae half space as normal
            DotP = torch.sum(torch.mul(vec, normal), dim=1)
            ValidIdx = DotP > 0.0
            InvalidIdx = DotP <= 0.0
            # vec = vec[ValidIdx]
            vec[InvalidIdx] = normal # Set invalid to just be normal
        return vec

    @staticmethod
    def prune_rays(Start, End, Vertices, thresh):
        ValidIdx = np.ones(len(Start), dtype=bool) * True
        RaysPerVertex = int(len(Start) / len(Vertices))

        for VIdx, p in enumerate(Vertices):
            Mask = np.ones(len(Start), dtype=bool)
            Mask[VIdx * RaysPerVertex:(VIdx+1) * RaysPerVertex] = False
            # Exclude the point itself
            a = Start[Mask]
            b = End[Mask]

            # https://stackoverflow.com/questions/54442057/calculate-the-euclidian-distance-between-an-array-of-points-to-a-line-segment-in/54442561#54442561
            # normalized tangent vector
            d = np.divide(b - a, np.linalg.norm(b - a, axis=0))

            # signed parallel distance components
            # s = np.dot(a - p, d)
            s = np.sum(np.multiply(a - p, d), axis=1)
            # t = np.dot(p - b, d)
            t = np.sum(np.multiply(p - b, d), axis=1)

            # clamped parallel distance
            h = np.maximum.reduce([s, t, np.zeros(len(s))])
            # perpendicular distance component
            c = np.cross(p - a, d)
            Distances = np.hypot(h, np.linalg.norm(c, axis=1))
            # print(np.min(Distances), np.max(Distances))
            ValidIdx[Mask] &= (Distances > thresh)

        return ValidIdx

    def sample(self, TargetRays, RatioPositive=0.8):
        nVertices = len(self.Vertices)
        TargetPositiveRays = int(TargetRays*RatioPositive)
        RaysPerVertex = int(TargetPositiveRays/nVertices)
        if RaysPerVertex <= 1:
            RaysPerVertex = 1
            TargetPositiveRays = RaysPerVertex*nVertices
        print('[ INFO ]: Aiming for {} positive and {} negative ray samples, {} rays per vertex.'.format(RaysPerVertex*nVertices, TargetRays-(RaysPerVertex*nVertices), RaysPerVertex))
        self.sample_positive(RaysPerVertex=RaysPerVertex)
        NegRaysPerVertex = int((TargetRays-TargetPositiveRays) / nVertices)
        self.sample_negative(RaysPerVertex=NegRaysPerVertex)

    def sample_negative(self, RaysPerVertex):
        # Numpy version - seems faster
        Tic = butils.getCurrentEpochTime()
        nVertices = len(self.Vertices)
        # Randomly offset vertices
        RandomDistances = np.random.uniform(PC_SAMPLER_NEG_MINOFFSET, PC_SAMPLER_NEG_MAXOFFSET, len(self.Vertices))
        Offsets = RandomDistances[:, np.newaxis] * self.VertexNormals
        # print(Offsets.shape)
        # print(np.linalg.norm(Offsets, axis=1)[:10])
        # print(RandomDistances[:10])
        OffsetVertices = self.Vertices + Offsets
        SampledDirections = np.zeros((RaysPerVertex * nVertices, 3))
        VertexRepeats = np.zeros((RaysPerVertex * nVertices, 3))
        ValidDirCtr = 0
        for VCtr in tqdm(range(nVertices)):
            ValidDirs = self.sample_directions_prune_numpy(RaysPerVertex, vertex=OffsetVertices, points=self.Vertices, thresh=PC_SAMPLER_THRESH)
            SampledDirections[ValidDirCtr:ValidDirCtr + len(ValidDirs)] = ValidDirs
            VertexRepeats[ValidDirCtr:ValidDirCtr + len(ValidDirs)] = self.Vertices[VCtr]
            ValidDirCtr += len(ValidDirs)

        SampledDirections = SampledDirections[:ValidDirCtr]
        VertexRepeats = VertexRepeats[:ValidDirCtr]
        print('[ INFO ]: Only able to sample {} valid rays out of {} requested.'.format(ValidDirCtr, RaysPerVertex * nVertices))

        # For each normal direction, find the point on a sphere of radius DEFAULT_RADIUS
        # Line-Sphere intersection: https://en.wikipedia.org/wiki/Line%E2%80%93sphere_intersection

        o = VertexRepeats
        u = SampledDirections
        c = np.array([0, 0, 0])
        OminusC = o - c
        DotP = np.sum(np.multiply(u, OminusC), axis=1)
        Delta = DotP ** 2 - (np.linalg.norm(OminusC, axis=1) - DEFAULT_RADIUS ** 2)
        d = - DotP + np.sqrt(Delta)
        SpherePoints = o + np.multiply(u, d[:, np.newaxis])

        Coordinates = np.asarray(np.hstack((SpherePoints, - u)))
        Intersects = np.asarray(np.zeros_like(d))
        Depths = np.asarray(np.zeros_like(d))
        ShuffleIdx = np.random.permutation(len(Coordinates))

        self.Coordinates = torch.from_numpy(Coordinates[ShuffleIdx]).to(torch.float32)
        self.Intersects = torch.from_numpy(Intersects[ShuffleIdx]).to(torch.float32)
        self.Depths = torch.from_numpy(Depths[ShuffleIdx])

        Toc = butils.getCurrentEpochTime()
        print('[ INFO ]: Numpy processed in {}ms.'.format((Toc - Tic) * 1e-3))

    def sample_positive(self, RaysPerVertex):
        # # Torch version
        # Device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
        # TVertices = torch.from_numpy(Verts).to(Device)
        # TVertexNormals = torch.from_numpy(VertNormals).to(Device)
        #
        # Tic = butils.getCurrentEpochTime()
        # nVertices = len(TVertices)
        # SampledDirections = torch.zeros((RaysPerVertex*nVertices, 3), dtype=TVertices.dtype, device=Device)
        # for VCtr in range(nVertices):
        #     SampledDirections[VCtr*RaysPerVertex:(VCtr+1)*RaysPerVertex] = self.sample_directions_torch(RaysPerVertex, normal=TVertexNormals[VCtr], device=Device, dtype=TVertices.dtype)
        #
        # Repeats = [RaysPerVertex]*nVertices
        # o = torch.repeat_interleave(TVertices, torch.tensor(Repeats, device=Device), dim=0)
        # # u = self.VertexNormals
        # u = SampledDirections
        # c = torch.zeros(1, 3).to(u.device)
        # OminusC = o - c
        # DotP = torch.sum(torch.mul(u, OminusC), dim=1)
        # Delta = DotP**2 - (torch.linalg.norm(OminusC, axis=1) - DEFAULT_RADIUS**2)
        # d = - DotP + torch.sqrt(Delta)
        # SpherePoints = o + torch.mul(u, d[:, None])
        #
        # self.Coordinates = torch.hstack((SpherePoints, - u)).to(torch.float32)
        # self.Intersects = torch.ones_like(d).to(torch.float32)
        # self.Depths = d.to(torch.float32)
        #
        # Toc = butils.getCurrentEpochTime()
        # print('[ INFO ]: pyTorch processed in {}ms.'.format((Toc-Tic)*1e-3))


        # Numpy version - seems faster
        Tic = butils.getCurrentEpochTime()
        nVertices = len(self.Vertices)
        SampledDirections = np.zeros((RaysPerVertex*nVertices, 3))
        VertexRepeats = np.zeros((RaysPerVertex*nVertices, 3))
        ValidDirCtr = 0
        for VCtr in tqdm(range(nVertices)):
            # SampledDirections[VCtr*RaysPerVertex:(VCtr+1)*RaysPerVertex] = self.sample_directions_numpy(RaysPerVertex, normal=self.VertexNormals[VCtr])
            ValidDirs = self.sample_directions_prune_normal_numpy(RaysPerVertex, vertex=self.Vertices[VCtr], normal=self.VertexNormals[VCtr], points=self.Vertices, thresh=PC_SAMPLER_THRESH)
            SampledDirections[ValidDirCtr:ValidDirCtr+len(ValidDirs)] = ValidDirs
            VertexRepeats[ValidDirCtr:ValidDirCtr+len(ValidDirs)] = self.Vertices[VCtr]
            ValidDirCtr += len(ValidDirs)

        SampledDirections = SampledDirections[:ValidDirCtr]
        VertexRepeats = VertexRepeats[:ValidDirCtr]
        print('[ INFO ]: Only able to sample {} valid rays out of {} requested.'.format(ValidDirCtr, RaysPerVertex*nVertices))

        # For each normal direction, find the point on a sphere of radius DEFAULT_RADIUS
        # Line-Sphere intersection: https://en.wikipedia.org/wiki/Line%E2%80%93sphere_intersection

        # Repeats = [RaysPerVertex] * nVertices
        # o = np.repeat(self.Vertices, Repeats, axis=0)
        # u = self.VertexNormals
        o = VertexRepeats
        u = SampledDirections
        c = np.array([0, 0, 0])
        OminusC = o - c
        DotP = np.sum(np.multiply(u, OminusC), axis=1)
        Delta = DotP**2 - (np.linalg.norm(OminusC, axis=1) - DEFAULT_RADIUS**2)
        d = - DotP + np.sqrt(Delta)
        SpherePoints = o + np.multiply(u, d[:, np.newaxis])

        Coordinates = np.asarray(np.hstack((SpherePoints, - u)))
        Intersects = np.asarray(np.ones_like(d))
        Depths = np.asarray(d)
        ShuffleIdx = np.random.permutation(len(Coordinates))

        self.Coordinates = torch.from_numpy(Coordinates[ShuffleIdx]).to(torch.float32)
        self.Intersects = torch.from_numpy(Intersects[ShuffleIdx]).to(torch.float32)
        self.Depths = torch.from_numpy(Depths[ShuffleIdx])

        Toc = butils.getCurrentEpochTime()
        print('[ INFO ]: Numpy processed in {}ms.'.format((Toc-Tic)*1e-3))


    def __getitem__(self, item):
        return self.Coordinate[item], (self.Intersects[item], self.Depths[item])

    def __len__(self):
        return len(self.Depths)

Parser = argparse.ArgumentParser()
Parser.add_argument('-i', '--input', help='Specify the input point cloud and normals in OBJ format.', required=True)
Parser.add_argument('-s', '--seed', help='Random seed.', required=False, type=int, default=42)
Parser.add_argument('-v', '--viz-limit', help='Limit visualizations to these many rays.', required=False, type=int, default=1000)
Parser.add_argument('-n', '--target-rays', help='Attempt to sample n rays per vertex.', required=False, type=int, default=100)

if __name__ == '__main__':
    Args = Parser.parse_args()
    butils.seedRandom(Args.seed)

    Mesh = trimesh.load(Args.input)
    Verts = Mesh.vertices
    Verts = odf_utils.mesh_normalize(Verts)
    VertNormals = Mesh.vertex_normals.copy()
    Norm = np.linalg.norm(VertNormals, axis=1)
    VertNormals /= Norm[:, None]

    Sampler = PointCloudSampler(Verts, VertNormals, TargetRays=Args.target_rays)

    app = QApplication(sys.argv)

    if len(Sampler) < Args.viz_limit:
        Args.viz_limit = len(Sampler)
    mainWindow = Easel([ODFDatasetLiveVisualizer(coord_type='direction', rays=Sampler.Coordinates.cpu(), intersects=Sampler.Intersects.cpu(), depths=Sampler.Depths.cpu(), DataLimit=Args.viz_limit)], sys.argv[1:])
    mainWindow.show()
    sys.exit(app.exec_())

