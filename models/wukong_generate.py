# Rough proxy in reference-image pixel coords: X = px, Z = 700 - py, Y = depth (+ = back)
import numpy as np, trimesh
from trimesh.creation import capsule, icosphere, cylinder

parts = []
def P(px, py, y=0): return [px, y, 700 - py]
def cap(a, b, r):
    a, b = np.array(a, float), np.array(b, float); v = b - a; L = np.linalg.norm(v)
    m = capsule(height=L, radius=r, count=[20, 20]); m.apply_translation([0, 0, L/2])
    m.apply_transform(trimesh.geometry.align_vectors([0, 0, 1], v / L)); m.apply_translation(a); parts.append(m)
def chain(pts, r):
    for a, b in zip(pts, pts[1:]): cap(a, b, r)
def ell(c, r, sub=3):
    m = icosphere(subdivisions=sub); m.apply_scale(r); m.apply_translation(c); parts.append(m)

# base plinth + rocks
b = cylinder(radius=1, height=22, sections=72); b.apply_scale([165, 130, 1]); b.apply_translation([430, 0, 11]); parts.append(b)
rng = np.random.default_rng(3)
for px, py, r in [(255,605,(40,40,35)), (320,640,(60,60,45)), (430,650,(75,75,35)),
                  (510,625,(70,55,50)), (570,645,(45,45,35)), (380,620,(40,50,40))]:
    ell(P(px, py, rng.uniform(-30, 30)), r, sub=1)
# legs (viewer-left = figure's right)
chain([P(382,440), P(362,520), P(355,598)], 20); ell(P(355,605,-15), (22,32,14))
chain([P(425,440), P(432,520), P(440,592)], 20); ell(P(440,600,-15), (22,32,14))
# pelvis/armoured skirt, belt, torso, neck, head, crown
ell(P(405,430), (55,38,55)); ell(P(405,372), (50,36,16))
ell(P(400,320), (50,32,62)); cap(P(400,265), P(400,225), 15)
ell(P(400,195), (28,28,31)); ell(P(400,215,-22), (16,10,14)); ell(P(400,170), (24,24,10))
# feathers
chain([P(398,158,5), P(412,100,8), P(425,40,10), P(432,6,12)], 3.5)
chain([P(392,160,5), P(372,110,8), P(345,78,10), P(322,72,12)], 3.5)
# pauldrons
ell(P(325,288), (32,30,30)); ell(P(472,288), (32,30,30))
# raised arm holding staff, hanging arm
chain([P(330,295), P(272,332,-5), P(243,265,15)], 14); ell(P(241,262,15), (16,16,18))
chain([P(470,295), P(497,362), P(520,425)], 13); ell(P(522,432), (13,13,17))
# staff (behind neck)
s0, s1 = np.array(P(75,295,15), float), np.array(P(628,178,15), float)
cap(s0, s1, 6); cap(s0, s0 + (s1-s0)*0.05, 8.5); cap(s1 - (s1-s0)*0.05, s1, 8.5)

# cape: lofted wavy sheet, closed solid
N, M, th = 60, 30, 5.0
u = np.linspace(0, 1, N)[:, None]; v = np.linspace(0, 1, M)[None, :]
tx, tz = 330 - 300*u, 400 - 65*u                  # top edge (shoulders -> tip)
bx, bz = 365 - 330*u, 95 + 210*u**1.3              # bottom edge (behind feet -> tip)
X = tx + (bx - tx)*v; Z = tz + (bz - tz)*v
Yc = 45 + 55*np.sin(2.6*np.pi*u + 1.5*v)*u + 25*v*(1-u)
front = np.stack([X, Yc - th, Z], -1).reshape(-1, 3); back = np.stack([X, Yc + th, Z], -1).reshape(-1, 3)
V = np.vstack([front, back]); off = N*M; idx = lambda i, j: i*M + j
F = []
for i in range(N-1):
    for j in range(M-1):
        a, b_, c, d = idx(i,j), idx(i+1,j), idx(i+1,j+1), idx(i,j+1)
        F += [[a,b_,c],[a,c,d],[off+a,off+c,off+b_],[off+a,off+d,off+c]]
ring = [idx(i,0) for i in range(N)] + [idx(N-1,j) for j in range(1,M)] + \
       [idx(i,M-1) for i in range(N-2,-1,-1)] + [idx(0,j) for j in range(M-2,0,-1)]
for k in range(len(ring)):
    a, b_ = ring[k], ring[(k+1) % len(ring)]
    F += [[a,off+a,off+b_],[a,off+b_,b_]]
cape = trimesh.Trimesh(V, np.array(F)); trimesh.repair.fix_normals(cape); parts.append(cape)
cap(P(330,300,40), P(330,300,5), 12)  # cape clasp joining it to the shoulder

m = trimesh.boolean.union(parts, engine='manifold')
lo, hi = m.bounds; m.apply_translation(-lo); m.apply_scale(np.array([100, 65, 109]) / (hi - lo))
print("bounds", m.extents.round(2), "watertight", m.is_watertight, "bodies", len(m.split()), "ml", round(m.volume/1000, 1))
m.export('/home/user/jump/models/wukong_100x65x109.stl')
