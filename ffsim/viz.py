# coding=utf-8
"""Live 3D view of the simulated printer: GET / for the page, GET /state?since=N for new path segments."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# [guess] carriages park at the far ends when idle; the decompile should say where the firmware parks them
PARK_X = {0: -110.0, 1: 110.0}

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FFCP2 Simulator</title>
<style>
:root{--bg:#f4f4f2;--fg:#1d1d1b;--muted:#6b6b66;--panel:rgba(255,255,255,.85)}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#e8e8e3;--muted:#9a9a93;--panel:rgba(20,20,20,.8)}}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:13px/1.4 ui-monospace,Menlo,monospace}
#hud{position:fixed;top:12px;left:12px;background:var(--panel);padding:10px 12px;border-radius:8px;min-width:230px}
#hud b{font-weight:600} .k{color:var(--muted)} label{display:block;margin-top:6px;color:var(--muted)}
.t0{color:#e8833a}.t1{color:#3a8be8}
</style></head><body>
<div id="hud">
<div><b>Creator Pro 2 (simulated)</b></div>
<div><span class="k">state</span> <span id="state">-</span></div>
<div><span class="k">tool</span> <span id="tool">-</span></div>
<div><span class="t0">T0</span> <span id="t0">-</span> &nbsp;<span class="t1">T1</span> <span id="t1">-</span> &nbsp;<span class="k">bed</span> <span id="bed">-</span></div>
<div><span class="k">segments</span> <span id="segs">0</span></div>
<label><input type="checkbox" id="travel"> show travel moves</label>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<script>
const scene=new THREE.Scene();
const dark=matchMedia('(prefers-color-scheme: dark)').matches;
scene.background=new THREE.Color(dark?0x141414:0xf4f4f2);
const camera=new THREE.PerspectiveCamera(45,innerWidth/innerHeight,1,5000);
camera.up.set(0,0,1);camera.position.set(-170,-260,220);
const renderer=new THREE.WebGLRenderer({antialias:true});
renderer.setPixelRatio(devicePixelRatio);renderer.setSize(innerWidth,innerHeight);document.body.appendChild(renderer.domElement);
const controls=new THREE.OrbitControls(camera,renderer.domElement);controls.target.set(0,0,20);controls.update();
scene.add(new THREE.AmbientLight(0xffffff,.7));const sun=new THREE.DirectionalLight(0xffffff,.6);sun.position.set(100,-200,300);scene.add(sun);

// build plate: 200 x 148 mm, center origin
const plate=new THREE.Mesh(new THREE.BoxGeometry(232,154,3),new THREE.MeshLambertMaterial({color:dark?0x5a4a2a:0xc9a96a}));
plate.position.z=-1.6;scene.add(plate);
const area=new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.PlaneGeometry(200,148)),new THREE.LineBasicMaterial({color:0x888880}));
area.position.z=.05;scene.add(area);

// x rail and two carriages with nozzles
const rail=new THREE.Mesh(new THREE.BoxGeometry(260,8,8),new THREE.MeshLambertMaterial({color:0x777777}));scene.add(rail);
function head(color){const g=new THREE.Group();
 const body=new THREE.Mesh(new THREE.BoxGeometry(34,30,30),new THREE.MeshLambertMaterial({color}));body.position.z=22;g.add(body);
 const noz=new THREE.Mesh(new THREE.ConeGeometry(3,7,16),new THREE.MeshLambertMaterial({color:0xd4af37}));noz.rotation.x=-Math.PI/2;noz.position.z=3.5;g.add(noz);
 scene.add(g);return g}
const heads=[head(0xe8833a),head(0x3a8be8)];

const colors=[new THREE.Color(0xe8833a),new THREE.Color(0x3a8be8)],travelColor=new THREE.Color(0x9a9a93);
const MAX=400000;
function layer(){const geo=new THREE.BufferGeometry();
 geo.setAttribute('position',new THREE.BufferAttribute(new Float32Array(MAX*6),3));
 geo.setAttribute('color',new THREE.BufferAttribute(new Float32Array(MAX*6),3));geo.setDrawRange(0,0);
 const l=new THREE.LineSegments(geo,new THREE.LineBasicMaterial({vertexColors:true}));scene.add(l);return {geo,l,n:0}}
const printed=layer(),travel=layer();travel.l.visible=false;
document.getElementById('travel').onchange=e=>travel.l.visible=e.target.checked;
function push(L,s,c){if(L.n>=MAX)return;const p=L.geo.attributes.position.array,col=L.geo.attributes.color.array,i=L.n*6;
 p.set([s[0],s[1],s[2],s[3],s[4],s[5]],i);col.set([c.r,c.g,c.b,c.r,c.g,c.b],i);L.n++;
 L.geo.setDrawRange(0,L.n*2);L.geo.attributes.position.needsUpdate=true;L.geo.attributes.color.needsUpdate=true}

let since=0;
async function poll(){try{const r=await fetch('/state?since='+since);const s=await r.json();
 for(const seg of s.segments){if(seg[7])push(printed,seg,colors[seg[6]]);else push(travel,seg,travelColor)}
 since=s.next;
 s.heads.forEach((h,i)=>heads[i].position.set(h[0],h[1],h[2]));rail.position.set(0,s.heads[0][1],s.heads[0][2]+22);
 const t=s.temps;document.getElementById('t0').textContent=t.T0.join('/');document.getElementById('t1').textContent=t.T1.join('/');
 document.getElementById('bed').textContent=t.B.join('/');document.getElementById('tool').textContent='T'+s.tool;
 document.getElementById('state').textContent=s.machine+' / '+s.move;document.getElementById('segs').textContent=since;
}catch(e){document.getElementById('state').textContent='simulator offline'}setTimeout(poll,100)}
poll();
addEventListener('resize',()=>{camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();renderer.setSize(innerWidth,innerHeight)});
(function loop(){requestAnimationFrame(loop);renderer.render(scene,camera)})();
</script></body></html>
"""


def snapshot(sim, since):
	with sim.lock:
		segments = sim.path[since:]
		pos = sim.pos
		heads = []
		for tool in (0, 1):
			x = pos["X"] if tool == sim.tool else PARK_X[tool]
			heads.append([x, pos["Y"], pos["Z"]])
		return dict(
			segments=segments,
			next=since + len(segments),
			heads=heads,
			tool=sim.tool,
			temps={k: [round(v[0]), round(v[1])] for k, v in sim.temps.items()},
			machine=sim.machine,
			move="PAUSED" if sim.paused else sim.move,
			control=sim.control,
			received=len(sim.received),
			recent=[line.decode("ascii", "replace") for line in sim.received[-15:]],
		)


def serve(sim, port=5099, host="127.0.0.1"):
	class Handler(BaseHTTPRequestHandler):
		def log_message(self, *args):
			pass

		def do_GET(self):
			url = urlparse(self.path)
			if url.path == "/state":
				since = int(parse_qs(url.query).get("since", ["0"])[0])
				body, kind = json.dumps(snapshot(sim, since)).encode(), "application/json"
			elif url.path == "/":
				body, kind = PAGE.encode(), "text/html; charset=utf-8"
			else:
				self.send_error(404)
				return
			self.send_response(200)
			self.send_header("Content-Type", kind)
			self.send_header("Content-Length", str(len(body)))
			self.end_headers()
			self.wfile.write(body)

	server = ThreadingHTTPServer((host, port), Handler)
	threading.Thread(target=server.serve_forever, name="ffsim.viz", daemon=True).start()
	return server
