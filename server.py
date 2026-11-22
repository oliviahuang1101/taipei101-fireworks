"""Taipei 101 visibility explorer v2. Run: python server.py"""
import os, json, math, re, threading, webbrowser
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
import requests
from shapely.geometry import Polygon, Point, LineString
from pyproj import Transformer

ROOT=Path(__file__).resolve().parent
app=Flask(__name__, static_folder=str(ROOT/'static'), static_url_path='')
LAT,LON=25.033968,121.564468
TO_M=Transformer.from_crs('EPSG:4326','EPSG:32651',always_xy=True).transform
TO_LL=Transformer.from_crs('EPSG:32651','EPSG:4326',always_xy=True).transform
CX,CY=TO_M(LON,LAT)
N=20; CELL=50
TARGETS=[120,165,210,255,300,345,390,435,480,525]
DEMO=[(-210,-80,80,110,75),(-120,100,85,65,110),(90,105,100,65,140),(160,-130,90,100,95),(-320,220,95,75,55),(300,190,90,70,85),(-70,-275,120,75,90),(260,-300,75,105,60),(-390,-100,65,85,50)]
DATA={}

def box(x,y,w,h):
 return Polygon([(x-w/2,y-h/2),(x+w/2,y-h/2),(x+w/2,y+h/2),(x-w/2,y+h/2)])
def demo():
 return [{'poly':box(x,y,w,h),'height':z,'source':'合成示範'} for x,y,w,h,z in DEMO]
def osm():
 south,north=LAT-.0052,LAT+.0052;west,east=LON-.0056,LON+.0056
 q=f'[out:json][timeout:35];way["building"]({south},{west},{north},{east});out geom tags;'
 errors=[]
 for url in ['https://overpass.kumi.systems/api/interpreter','https://overpass-api.de/api/interpreter','https://overpass.nchc.org.tw/api/interpreter']:
  try:
   resp=requests.post(url,data={'data':q},timeout=40);resp.raise_for_status()
   result=[]
   for item in resp.json().get('elements',[]):
    pts=item.get('geometry',[])
    if len(pts)<4:continue
    poly=Polygon([(x-CX,y-CY) for x,y in (TO_M(p['lon'],p['lat']) for p in pts)])
    if not poly.is_valid:poly=poly.buffer(0)
    if poly.is_empty or poly.geom_type!='Polygon' or poly.area<12:continue
    tags=item.get('tags',{});height=None
    try:
     if tags.get('height'):height=float(re.search(r'\d+(?:\.\d+)?',tags['height']).group())
     elif tags.get('building:levels'):height=float(re.search(r'\d+(?:\.\d+)?',tags['building:levels']).group())*3
    except (AttributeError,ValueError):pass
    result.append({'poly':poly,'height':height,'source':'OSM'})
   if result:return result
  except Exception as e:errors.append(type(e).__name__)
 raise RuntimeError('OSM 資料取得失敗：'+', '.join(errors))

def visibility(x,y,h,buildings):
 line=LineString([(x,y),(0,0)]); length=line.length
 if length<1:return True
 uncertain=False
 for b in buildings:
  poly=b['poly']
  if poly.contains(Point(0,0)) or not poly.intersects(line):continue
  hit=poly.intersection(line)
  if hit.is_empty:continue
  pieces=[]
  def coords(g):
   if g.geom_type=='Point':pieces.append(g.coords[0])
   elif g.geom_type in ('LineString','LinearRing'):pieces.extend(g.coords)
   elif hasattr(g,'geoms'):
    for sub in g.geoms:coords(sub)
  coords(hit)
  ts=[line.project(Point(p))/length for p in pieces]
  ts=[t for t in ts if .005<t<.995]
  if not ts:continue
  if b['height'] is None:uncertain=True;continue
  if b['height']>=1.6+(h-1.6)*min(ts):return False
 return None if uncertain else True

def build(mode):
 buildings=osm() if mode=='osm' else demo()
 cells=[]
 for i in range(N):
  for j in range(N):
   x=(i-N/2+.5)*CELL;y=(j-N/2+.5)*CELL
   if any(b['poly'].contains(Point(x,y)) for b in buildings):continue
   vals=[visibility(x,y,h,buildings) for h in TARGETS];known=[v for v in vals if v is not None]
   score=round(100*sum(known)/len(known)) if known else None
   lon,lat=TO_LL(CX+x,CY+y)
   dist=math.hypot(x,y)
   crowd='未知' if mode=='osm' else ('非常擠' if dist<210 else '稍擠' if dist<380 else '舒適')
   walk=None if mode=='osm' else round(5+dist/100)
   cells.append({'id':f'G{i:02d}-{j:02d}','x':x,'y':y,'lat':lat,'lon':lon,'score':score,'uncertain':len(known)<len(vals),'crowd':crowd,'walk':walk})
 polys=[]
 for b in buildings:
  poly=b['poly']
  if poly.geom_type!='Polygon':continue
  coords=list(poly.exterior.coords)[:-1]
  if len(coords)>100:coords=list(poly.simplify(2).exterior.coords)[:-1]
  polys.append({'points':[[round(x,2),round(y,2)] for x,y in coords],'height':b['height'],'source':b['source']})
 return {'mode':mode,'cells':cells,'buildings':polys,'unknown':sum(b['height'] is None for b in buildings),'total':len(buildings)}

# Initialize demo data for Gunicorn and local Python execution.
DATA['current'] = build('demo')

def current():return DATA['current']

@app.route('/')
def index():return send_from_directory(ROOT/'static','index.html')
@app.route('/api/data')
def data():return jsonify(current())
@app.route('/api/reload',methods=['POST'])
def reload():
 mode=(request.get_json(silent=True) or {}).get('mode','demo')
 if mode not in ('demo','osm'):return jsonify(error='無效模式'),400
 try:
  result=build(mode);DATA['current']=result;return jsonify(result)
 except Exception as e:return jsonify(error=str(e)),503

@app.route('/api/recommend',methods=['POST'])
def recommend():
 req=request.get_json(silent=True) or {};query=str(req.get('query',''))[:600];d=current();cells=d['cells']
 # Transparent rule-based fallback. No claim of LLM without an API key.
 wants_quiet=any(k in query for k in ['人少','不擠','不要擠','安靜','舒適','避開人潮'])
 wants_view=any(k in query for k in ['視野','看得','清楚','完整','煙火'])
 walk_match=re.search(r'(?:步行|走路|捷運).{0,12}?(\d{1,2})\s*分鐘',query)
 maxwalk=int(walk_match.group(1)) if walk_match else None
 ranked=[]
 for c in cells:
  if c['score'] is None:continue
  if d['mode']=='osm' and (wants_quiet or maxwalk is not None):continue # unavailable attributes are never fabricated
  value=(c['score'] or 0)*1.3 - (22 if c['uncertain'] else 0)
  if wants_quiet:value+={'舒適':45,'稍擠':5,'非常擠':-50}.get(c['crowd'],0)
  if maxwalk is not None and c['walk'] is not None:value-=max(0,c['walk']-maxwalk)*18
  ranked.append((value,c))
 ranked.sort(key=lambda item:item[0],reverse=True)
 picks=[]
 for _,c in ranked:
  if any(math.hypot(c['x']-p['x'],c['y']-p['y'])<120 for p in picks):continue
  picks.append(c)
  if len(picks)==3:break
 note='使用規則式條件排序，非 LLM。所有示範模式的人潮及步行時間均為合成值。'
 if d['mode']=='osm' and (wants_quiet or maxwalk is not None):note='OSM 模式缺乏人潮與捷運步行時間，無法可靠滿足此需求；不提供虛構推薦。'
 return jsonify(items=picks,note=note)

if __name__=='__main__':
 DATA['current']=build('demo')
 print('Open http://127.0.0.1:7860')
 threading.Timer(1.4,lambda:webbrowser.open('http://127.0.0.1:7860')).start()
 app.run(host='0.0.0.0',port=int(os.getenv('PORT','7860')),debug=False)
