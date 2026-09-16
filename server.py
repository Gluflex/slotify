"""Local web front end for step2kit. Standard library only: python server.py  ->  http://127.0.0.1:4790

Upload a STEP, enter the laser kerf, get a nested DXF plus previews and checks. Everything runs locally,
deterministically, with no network access and no machine learning.
"""
from __future__ import annotations

import argparse
import base64
import email
import email.policy
import html
import json
import os
import sys
import threading
import time
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

# Frozen (PyInstaller) build: there is no separate python.exe to spawn a job subprocess with, and the
# app's own install folder may not be writable (e.g. under Program Files), so jobs live under the user's
# local app data instead of next to the executable, and step2kit is already importable without a
# sys.path hack (PyInstaller bundles it into the frozen interpreter directly).
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    HERE = os.path.dirname(sys.executable)
    JOBS_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "step2kit", "jobs")
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, HERE)
    JOBS_DIR = os.path.join(HERE, "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

from step2kit.pipeline import Config   # noqa: E402

MIME = {".dxf": "application/dxf", ".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json",
        ".step": "application/step", ".stp": "application/step", ".txt": "text/plain; charset=utf-8",
        ".html": "text/html; charset=utf-8"}

_FAVICON_SVG = (b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
                b"<rect width='64' height='64' rx='14' fill='#13152d'/>"
                b"<path d='M14 42 L32 14 L50 42' fill='none' stroke='#ffffff' stroke-width='5' "
                b"stroke-linecap='round' stroke-linejoin='round'/>"
                b"<rect x='14' y='42' width='36' height='9' rx='2' fill='#ffffff'/></svg>")
FAVICON = "data:image/svg+xml;base64," + base64.b64encode(_FAVICON_SVG).decode()

FIELDS = [  # name, label, default, help, section
    ("kerf", "Laser kerf (beam width) in mm", "0.2", "Each outline is offset by half of this. Measure it on your machine with a test cut.", "Sheet & cutting"),
    ("thickness", "Sheet thickness override in mm", "", "Leave empty to use the thickness found in the model. Set it when the real plywood differs (e.g. 2.8).", "Sheet & cutting"),
    ("sheet_w", "Sheet width in mm", "600", "", "Sheet & cutting"),
    ("sheet_h", "Sheet height in mm", "900", "", "Sheet & cutting"),
    ("gap", "Gap between parts in mm", "5", "", "Sheet & cutting"),
    ("margin", "Sheet margin in mm", "10", "", "Sheet & cutting"),
    ("min_feature", "Flag features narrower than (mm)", "1.0", "Thin slivers are marked in red on the previews.", "Sheet & cutting"),
    ("finger", "Finger / tab width in mm", "", "Default 2 x thickness.", "Joints"),
    ("finger_min", "Minimum finger width in mm", "", "Default = thickness. Bars shorter than twice this get no joint.", "Joints"),
    ("play", "Joint fit slack in mm", "0", "Narrows tabs (not slots) by this much total, independent of kerf. 0 = as tight as the kerf setting allows; raise it for a looser, glue-friendly fit.", "Joints"),
    ("waste_tabs", "Waste tabs per part", "0", "Small uncut bridges holding each part to the surrounding sheet so it can't shift or drop mid-cut. 0 = disabled.", "Sheet & cutting"),
    ("waste_tab_len", "Waste tab length in mm", "2", "Length of each bridge; only used when waste tabs are enabled above.", "Sheet & cutting"),
    ("wall", "Wall thickness for filled bodies in mm", "", "Used only when a part is a solid block rather than a shell; a closed shell is hollowed out to this thickness automatically. Default: sheet thickness, or 3 mm.", "Filled bodies & curves"),
    ("facet_chord", "Curved-surface chord tolerance in mm", "0.5", "Cylinders, fillets and other curved faces are approximated as flat facets. Smaller = more facets, closer to the real curve.", "Filled bodies & curves"),
    ("facet_angle", "Curved-surface angle tolerance in degrees", "30", "Larger = fewer, bigger facets on curved surfaces (lower resolution).", "Filled bodies & curves"),
]

# Small pictograms next to each field label -- inline SVG paths, not full <svg> tags (field_icon() wraps
# them). Several of these parameters are hard to picture from a text description alone (what does a
# "chord tolerance" actually look like on a curve?), so each icon sketches the geometric idea it controls
# rather than just decorating the label.
FIELD_ICONS = {
    "kerf": "<line x1='7' y1='2' x2='7' y2='18'/><line x1='13' y1='2' x2='13' y2='18'/>"
            "<line x1='9' y1='10' x2='11' y2='10'/><path d='M9.8 8.8L9 10l.8 1.2M10.2 8.8L11 10l-.8 1.2'/>",
    "thickness": "<rect x='2.5' y='8' width='11' height='4'/><line x1='16.5' y1='8' x2='16.5' y2='12'/>"
                 "<path d='M15.7 8.8L16.5 8l.8.8M15.7 11.2l.8.8.8-.8'/>",
    "sheet_w": "<rect x='3' y='6' width='14' height='10'/><line x1='3' y1='3' x2='17' y2='3'/>"
               "<path d='M3.8 2.2L3 3l.8.8M16.2 2.2L17 3l-.8.8'/>",
    "sheet_h": "<rect x='4' y='3' width='10' height='14'/><line x1='17' y1='3' x2='17' y2='17'/>"
               "<path d='M16.2 3.8L17 3l.8.8M16.2 16.2l.8.8.8-.8'/>",
    "gap": "<rect x='2' y='6' width='6' height='8'/><rect x='12' y='6' width='6' height='8'/>"
           "<line x1='9' y1='10' x2='11' y2='10'/><path d='M9.8 8.8L9 10l.8 1.2M10.2 8.8L11 10l-.8 1.2'/>",
    "margin": "<rect x='2' y='2' width='16' height='16'/><rect x='5.5' y='5.5' width='9' height='9' stroke-opacity='0.45'/>",
    "min_feature": "<rect x='2' y='13' width='10' height='4'/><path d='M12 13l6-1-6-1z'/>",
    "finger": "<path d='M2 5h3v4h3V5h3v4h3V5h3'/>",
    "finger_min": "<path d='M2 5h3v4h3V5h3v4h3V5h3'/>",
    # a slot (faint outer rect) with a narrower tab (solid inner rect) inside it, and tick marks in the
    # gap between them -- play widens exactly that gap, independent of the kerf icon's beam-width ticks
    "play": "<rect x='3' y='3' width='14' height='14' rx='1' stroke-opacity='0.45'/>"
            "<rect x='6.5' y='6.5' width='7' height='7'/>"
            "<line x1='4.3' y1='10' x2='5.8' y2='10' stroke-width='2.2'/>"
            "<line x1='14.2' y1='10' x2='15.7' y2='10' stroke-width='2.2'/>",
    # a part outline with two short stubs poking through its boundary -- the uncut bridges to scrap
    "waste_tabs": "<rect x='3' y='3' width='14' height='14' rx='1'/>"
                  "<line x1='3' y1='7' x2='0.5' y2='7' stroke-width='2.2'/>"
                  "<line x1='17' y1='13' x2='19.5' y2='13' stroke-width='2.2'/>",
    "wall": "<rect x='2.5' y='2.5' width='15' height='15' rx='1'/><rect x='6' y='6' width='8' height='8' rx='.5' stroke-opacity='0.45'/>",
    # arc + its straight chord + a short solid tick marking the gap (sagitta) between them -- exactly
    # what "chord tolerance" caps; dashed lines vanish at 18px so the tick is a plain thick segment instead
    "facet_chord": "<path d='M2.5 14 Q10 2.5 17.5 14'/><line x1='2.5' y1='14' x2='17.5' y2='14'/>"
                   "<line x1='10' y1='14' x2='10' y2='7' stroke-width='2.2'/>",
    # the same curve approximated by straight facets, with the kink angle at one joint marked by a small arc
    "facet_angle": "<path d='M2.5 16.5 L8 9 L13 7.5 L17.5 2.5'/><path d='M9.6 8.7 A2.3 2.3 0 0 0 8.3 10.9'/>",
}


def field_icon(name):
    paths = FIELD_ICONS.get(name)
    if not paths:
        return ""
    return (f"<svg class='ficon' viewBox='0 0 20 20' width='18' height='18' fill='none' stroke='currentColor' "
            f"stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'>{paths}</svg>")

PAGE_CSS = """
:root{
  --bg:#f6f3ec; --bg-elev:#ffffff; --border:#e2dbc9; --border-soft:#ece6d8;
  --ink:#1c1c1c; --ink-soft:#6b6559; --ink-faint:#8f897c;
  --navy:#13152d; --accent:#525da6; --accent-soft:#e9ebf6;
  --btn-bg:#13152d; --btn-fg:#ffffff;
  --amber:#8a5a06; --amber-bg:#fff3cd; --amber-border:#f0cf7a;
  --green:#1f7a3d; --green-bg:#e3f4e1; --green-border:#9ed39a;
  --red:#b3261e; --red-bg:#fde2e1; --red-border:#f0a5a2;
  --radius:10px; --radius-sm:6px;
  --shadow:0 1px 2px rgba(20,18,10,.05), 0 6px 20px rgba(20,18,10,.06);
  --mono:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",Menlo,monospace;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#14161c; --bg-elev:#1b1e27; --border:#2c303c; --border-soft:#242732;
    --ink:#e9e7e0; --ink-soft:#a9a59c; --ink-faint:#7f7c74;
    --navy:#9aa1e0; --accent:#9aa1e0; --accent-soft:#242a45;
    --btn-bg:#9aa1e0; --btn-fg:#12131c;
    --amber:#e8c25a; --amber-bg:#332a10; --amber-border:#5f4c1c;
    --green:#6fdb92; --green-bg:#12301d; --green-border:#245c37;
    --red:#f4948e; --red-bg:#3a1616; --red-border:#6b2726;
    --shadow:0 1px 2px rgba(0,0,0,.35), 0 8px 24px rgba(0,0,0,.4);
  }
}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Arial,sans-serif;margin:0;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
main{max-width:1100px;margin:0 auto;padding:28px 20px 64px}
header.top{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:6px}
.brand{display:flex;align-items:center;gap:10px}
.brand .mark{width:28px;height:28px;border-radius:8px;background:var(--navy);flex:0 0 auto;box-shadow:var(--shadow)}
h1{font-weight:650;font-size:21px;margin:0;letter-spacing:-.01em}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{font-size:11px;color:var(--ink-soft);border:1px solid var(--border);border-radius:999px;padding:3px 10px;background:var(--bg-elev)}
.sub{color:var(--ink-soft);margin:6px 0 22px;font-size:14px;line-height:1.55;max-width:72ch}
.card{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow)}
form.card{padding:20px}
.tabbar{display:flex;gap:4px;border-bottom:1px solid var(--border);margin-bottom:18px;flex-wrap:wrap}
.tabbtn{background:none;border:0;padding:10px 16px;font-size:14px;font-weight:650;color:var(--ink-soft);cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;transition:color .15s,border-color .15s}
.tabbtn:hover{color:var(--ink)}
.tabbtn.active{color:var(--accent);border-bottom-color:var(--accent)}
.tabpanel[hidden]{display:none}
.panel{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);padding:20px;margin:18px 0}
.panel h2{margin:0 0 4px}
.panel fieldset{margin:0 0 16px}
.panel fieldset:last-of-type{margin-bottom:16px}
.inline-form{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}
select{width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:var(--radius-sm);font-size:14px;background:var(--bg);color:var(--ink)}
select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
input[type=file]{width:100%;padding:7px 0;font-size:12.5px;color:var(--ink-soft)}
input[type=file]::file-selector-button{background:var(--bg-elev);color:var(--ink);border:1px solid var(--border);border-radius:var(--radius-sm);padding:7px 14px;font-size:12.5px;font-weight:600;cursor:pointer;margin-right:10px;transition:border-color .15s}
input[type=file]::file-selector-button:hover{border-color:var(--accent)}
fieldset{border:0;margin:0 0 18px;padding:0}
fieldset:last-of-type{margin-bottom:0}
legend{font-size:11.5px;font-weight:650;color:var(--ink-soft);text-transform:uppercase;letter-spacing:.05em;padding:0 0 8px;margin-bottom:10px;border-bottom:1px solid var(--border-soft);width:100%}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
label{display:block;font-size:12.5px;color:var(--ink-soft);margin-bottom:4px;font-weight:500}
.flabel{display:flex;align-items:center;gap:6px}
.ficon{color:var(--ink-faint);flex:0 0 auto}
.progress{height:8px;border-radius:999px;background:var(--border-soft);overflow:hidden;margin:4px 0 16px;position:relative}
.progress-fill{height:100%;border-radius:999px;background:linear-gradient(90deg,var(--accent),var(--navy));transition:width .5s ease;position:relative;min-width:4%}
.progress-fill::after{content:'';position:absolute;inset:0;background-image:linear-gradient(135deg,rgba(255,255,255,.28) 25%,transparent 25%,transparent 50%,rgba(255,255,255,.28) 50%,rgba(255,255,255,.28) 75%,transparent 75%,transparent);background-size:16px 16px;animation:barstripes 0.9s linear infinite}
@keyframes barstripes{from{background-position:0 0}to{background-position:16px 0}}
.stepline{font-size:13px;color:var(--ink-soft);margin:2px 0 8px;min-height:18px}
input[type=text],input[type=number]{width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:var(--radius-sm);font-size:14px;background:var(--bg);color:var(--ink);transition:border-color .15s,box-shadow .15s}
input[type=text]:focus,input[type=number]:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.help{font-size:11px;color:var(--ink-faint);margin-top:3px;line-height:1.4}
.dropzone{border:1.5px dashed var(--border);border-radius:var(--radius);padding:24px;text-align:center;cursor:pointer;background:var(--bg);transition:border-color .15s,background .15s;margin-bottom:18px}
.dropzone:hover,.dropzone.drag{border-color:var(--accent);background:var(--accent-soft)}
.dropzone svg{color:var(--ink-faint)}
.dropzone .dz-text{font-size:14px;color:var(--ink);margin-top:8px}
.dropzone .dz-text b{color:var(--accent)}
.dropzone .dz-file{margin-top:6px;font-size:12.5px;color:var(--ink-soft);font-family:var(--mono)}
.switches{display:flex;flex-wrap:wrap;gap:14px 24px;margin:6px 0 18px}
.switch{display:inline-flex;align-items:center;gap:9px;cursor:pointer;font-size:13.5px;color:var(--ink)}
.switch input{position:absolute;opacity:0;width:0;height:0}
.switch .track{width:34px;height:20px;border-radius:20px;background:var(--border);position:relative;transition:background .15s;flex:0 0 auto}
.switch .track::after{content:'';position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.25);transition:transform .15s}
.switch input:checked + .track{background:var(--accent)}
.switch input:checked + .track::after{transform:translateX(14px)}
button[type=submit]{background:var(--btn-bg);color:var(--btn-fg);border:0;border-radius:var(--radius-sm);padding:11px 22px;font-size:14.5px;font-weight:600;cursor:pointer;transition:opacity .15s;box-shadow:var(--shadow)}
button[type=submit]:hover{opacity:.88}
button[type=submit]:disabled{opacity:.6;cursor:default}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;font-weight:650;padding:3px 10px;border-radius:999px;text-transform:uppercase;letter-spacing:.03em;border:1px solid transparent}
.badge-running{background:var(--amber-bg);color:var(--amber);border-color:var(--amber-border)}
.badge-running .dot{width:6px;height:6px;border-radius:50%;background:var(--amber);animation:pulse 1.2s infinite}
.badge-done{background:var(--green-bg);color:var(--green);border-color:var(--green-border)}
.badge-error{background:var(--red-bg);color:var(--red);border-color:var(--red-border)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.alert{border-radius:var(--radius);padding:12px 14px;margin:14px 0;font-size:13.5px;line-height:1.55;border:1px solid}
.alert.warn{background:var(--amber-bg);border-color:var(--amber-border)}
.alert.ok{background:var(--green-bg);border-color:var(--green-border)}
.alert.err{background:var(--red-bg);border-color:var(--red-border)}
.alert ul{margin:8px 0 0;padding-left:18px}
.alert li{margin:2px 0}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:16px 0}
.stat{background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px}
.stat .n{font-size:21px;font-weight:650;color:var(--accent)}
.stat .l{font-size:11px;color:var(--ink-soft);text-transform:uppercase;letter-spacing:.04em;margin-top:2px}
.files{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}
.files a{display:inline-flex;align-items:center;gap:6px;padding:9px 16px;background:var(--btn-bg);color:var(--btn-fg);border-radius:var(--radius-sm);text-decoration:none;font-size:13.5px;font-weight:600;box-shadow:var(--shadow);transition:opacity .15s}
.files a:hover{opacity:.88}
.files a.sec{background:var(--bg-elev);color:var(--ink);border:1px solid var(--border);box-shadow:none}
.files a.sec:hover{border-color:var(--accent);opacity:1;background:var(--accent-soft)}
.preview-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin:10px 0 4px}
figure.prevfig{margin:0;background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);padding:10px;box-shadow:var(--shadow)}
figure.prevfig img{width:100%;height:auto;border-radius:6px;display:block;background:#fff}
figure.prevfig figcaption{font-size:11.5px;color:var(--ink-soft);margin-top:6px;text-align:center}
.svgwrap{background:#fff;border:1px solid var(--border);border-radius:var(--radius);margin:10px 0;overflow:auto;padding:8px;box-shadow:var(--shadow)}
.svgwrap svg{width:100%;height:auto;display:block}
.tablewrap{overflow-x:auto;border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow)}
table{border-collapse:collapse;width:100%;font-size:13px;background:var(--bg-elev)}
th,td{padding:8px 10px;text-align:left;vertical-align:top;border-bottom:1px solid var(--border-soft)}
th{background:var(--bg);font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;color:var(--ink-soft);font-weight:650}
tbody tr:hover{background:var(--accent-soft)}
tbody tr:last-child td{border-bottom:0}
.kind{display:inline-block;font-size:11px;font-weight:650;padding:2px 9px;border-radius:999px;text-transform:uppercase;letter-spacing:.03em}
.kind-finger{background:var(--accent-soft);color:var(--accent)}
.kind-tab{background:var(--green-bg);color:var(--green)}
.kind-crosslap{background:var(--amber-bg);color:var(--amber)}
.kind-none{background:var(--border-soft);color:var(--ink-faint)}
pre.console{background:#14161c;color:#d8d5cb;padding:14px 16px;border-radius:var(--radius);font-size:12px;line-height:1.55;max-height:420px;overflow:auto;white-space:pre-wrap;font-family:var(--mono);box-shadow:var(--shadow);margin:0}
.joblist{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}
.joblist li{display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius-sm);padding:9px 12px;font-size:13px}
.joblist a{color:var(--ink);text-decoration:none;font-weight:550}
.joblist a:hover{color:var(--accent)}
.joblist .meta{color:var(--ink-faint);font-size:11.5px;display:flex;align-items:center;gap:8px;white-space:nowrap}
h2{font-size:15px;margin:26px 0 10px;font-weight:650;letter-spacing:-.005em}
details.block summary{cursor:pointer;font-size:15px;font-weight:650;color:var(--ink);padding:4px 0;list-style:none}
details.block summary::-webkit-details-marker{display:none}
details.block summary::before{content:'\\25B8';display:inline-block;margin-right:6px;color:var(--ink-faint);transition:transform .15s}
details.block[open] summary::before{transform:rotate(90deg)}
ol.steps{padding-left:20px;color:var(--ink-soft);font-size:13.5px;line-height:1.7;margin:10px 0 0}
ol.steps li{margin:4px 0}
code{background:var(--accent-soft);color:var(--accent);padding:1px 5px;border-radius:4px;font-family:var(--mono);font-size:12px}
@media(max-width:640px){header.top{flex-direction:column;align-items:flex-start}}
"""


def page(title, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<link rel='icon' href='{FAVICON}'>"
            f"<title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head>"
            f"<body><main>{body}</main></body></html>")


STATE_BADGE = {
    "running": "<span class='badge badge-running'><span class='dot'></span>running</span>",
    "done": "<span class='badge badge-done'>done</span>",
    "error": "<span class='badge badge-error'>error</span>",
}


def index_html(jobs):
    sections = {}
    for name, label, default, helptext, section in FIELDS:
        sections.setdefault(section, []).append(
            f"<div><label class='flabel' for='{name}'>{field_icon(name)}{html.escape(label)}</label>"
            f"<input type='text' id='{name}' name='{name}' value='{default}'>"
            + (f"<div class='help'>{html.escape(helptext)}</div>" if helptext else "") + "</div>")
    fieldsets = "".join(
        f"<fieldset><legend>{html.escape(section)}</legend><div class='grid'>{''.join(items)}</div></fieldset>"
        for section, items in sections.items())
    recent = ""
    if jobs:
        rows = "".join(
            f"<li><a href='/jobs/{j['id']}'>{html.escape(j.get('input', j['id']))}</a>"
            f"<span class='meta'>{STATE_BADGE.get(j.get('state',''), html.escape(j.get('state','')))} {html.escape(j.get('when',''))}</span></li>"
            for j in jobs)
        recent = f"<h2>Previous jobs</h2><ul class='joblist'>{rows}</ul>"
    body = f"""
<header class='top'>
  <div class='brand'><span class='mark'></span><h1>Slotify</h1></div>
  <div class='chips'><span class='chip'>deterministic</span><span class='chip'>no AI</span><span class='chip'>runs locally</span></div>
</header>
<div class='sub'>Upload a plate-based STEP model, generate a box from scratch, or check your laser's kerf &mdash; deterministic,
no AI, runs locally.</div>
<div class='tabbar'>
  <button type='button' class='tabbtn active' data-tab='tab-step'>Upload STEP</button>
  <button type='button' class='tabbtn' data-tab='tab-box'>Generate a box</button>
  <button type='button' class='tabbtn' data-tab='tab-kerf'>Kerf test</button>
</div>
<div class='tabpanel' id='tab-step'>
<form class='card' method='post' action='/jobs' enctype='multipart/form-data' id='kitform'>
  <div class='dropzone' id='dz'>
    <input type='file' id='step' name='step' accept='.step,.stp,.STEP,.STP' required hidden>
    <svg width='30' height='30' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.6'><path d='M12 3v12m0 0l-4-4m4 4l4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2'/></svg>
    <div class='dz-text'><b>Click to choose</b> or drag a STEP file here</div>
    <div class='dz-file' id='dz-file'></div>
  </div>
  {fieldsets}
  <div class='switches'>
    <label class='switch'><input type='checkbox' name='labels' checked><span class='track'></span>engrave part names (ETCH layer)</label>
    <label class='switch'><input type='checkbox' name='assembly' checked><span class='track'></span>find an assembly order (slower)</label>
    <label class='switch'><input type='checkbox' name='auto_shell' checked><span class='track'></span>auto-shell filled (solid) bodies</label>
    <label class='switch'><input type='checkbox' name='auto_facet' checked><span class='track'></span>facet curved surfaces (cylinders etc.)</label>
  </div>
  <button type='submit' id='submitbtn'>Generate kit</button>
</form>
<details class='block'>
<summary>What it does</summary>
<ol class='steps'>
<li>If a part is a solid block rather than a shell, hollows it out to a wall thickness automatically. If it has curved surfaces (cylinders, fillets, ...), approximates them as flat facets at the chosen resolution.</li>
<li>Finds every constant-thickness plate in the model (a single shelled solid is decomposed into its walls).</li>
<li>For every pair of plates that meet, classifies the interface from the geometry: corner, T-junction, cross-lap, at any angle.</li>
<li>Cuts finger joints, tabs and slots as perpendicular prisms, so every part is cuttable as drawn.</li>
<li>Flattens, applies the kerf, checks for collisions, thin slivers, rebuilds the 3D kit from the flat outlines, and searches an assembly order.</li>
<li>Nests the parts on your sheet and writes DXF (layers CUT_OUTER, CUT_INNER, ETCH, SHEET), SVG, PNG previews, STEP of the kit and a JSON report.</li>
</ol>
</details>
</div>
<div class='tabpanel' id='tab-kerf' hidden>
<div class='panel'>
  <h2>Not sure what kerf to use?</h2>
  <div class='sub' style='margin:0 0 12px'>One reference plate with a row of labeled slots, each pre-compensated for a candidate
  kerf value, plus a separate loose tab for each value. Cut it once on scrap and try each tab against the plate by hand &mdash;
  because the tabs are independent pieces, a too-tight one never blocks you from trying the others. Whichever tab seats snugly
  (not forced, not loose): read its label, that's your kerf. Use that number in the Upload / Box tabs above.</div>
  <form method='get' action='/kerf-test' class='inline-form'>
    <button type='submit'>Download kerf test piece</button>
  </form>
</div>
</div>
<div class='tabpanel' id='tab-box' hidden>
<div class='panel'>
  <h2>Or generate a box</h2>
  <div class='sub' style='margin:0 0 12px'>No STEP file needed: give it outer dimensions and a wall thickness, get a finger-jointed
  box kit built by the exact same joint-finding pipeline as an imported model.</div>
  <form method='post' action='/jobs/box' enctype='multipart/form-data'>
    <fieldset><legend>Dimensions</legend><div class='grid'>
      <div><label for='box_w'>Width (X) in mm</label><input type='text' id='box_w' name='box_w' value='200'></div>
      <div><label for='box_d'>Depth (Y) in mm</label><input type='text' id='box_d' name='box_d' value='150'></div>
      <div><label for='box_h'>Height (Z) in mm</label><input type='text' id='box_h' name='box_h' value='80'></div>
      <div><label for='box_wall'>Wall thickness in mm</label><input type='text' id='box_wall' name='box_wall' value='3'></div>
      <div><label for='box_kerf'>Laser kerf in mm</label><input type='text' id='box_kerf' name='box_kerf' value='0.2'></div>
    </div></fieldset>
    <fieldset><legend>Lid</legend><div class='grid'>
      <div><label for='box_lid'>Lid style</label><select id='box_lid' name='box_lid'>
        <option value='closed' selected>Closed (solid top)</option>
        <option value='open'>Open (no lid)</option>
        <option value='slip'>Slip (friction-fit flat lid)</option>
        <option value='slide'>Slide (captured on glued rails)</option>
      </select></div>
    </div></fieldset>
    <fieldset><legend>Dividers</legend><div class='grid'>
      <div><label for='box_div_x'>Dividers along X</label><input type='text' id='box_div_x' name='box_div_x' value='0'></div>
      <div><label for='box_div_y'>Dividers along Y</label><input type='text' id='box_div_y' name='box_div_y' value='0'></div>
    </div></fieldset>
    <fieldset><legend>Handle &amp; vents</legend>
      <div class='switches' style='margin:0 0 12px'>
        <label class='switch'><input type='checkbox' name='box_handle'><span class='track'></span>hand-hole in the two end walls</label>
      </div>
      <div class='grid'>
        <div><label for='box_vent_face'>Vent face</label><select id='box_vent_face' name='box_vent_face'>
          <option value='' selected>None</option>
          <option value='front'>Front</option><option value='back'>Back</option>
          <option value='left'>Left</option><option value='right'>Right</option>
          <option value='top'>Top</option><option value='bottom'>Bottom</option>
        </select></div>
        <div><label for='box_vent_rows'>Vent rows</label><input type='text' id='box_vent_rows' name='box_vent_rows' value='0'></div>
        <div><label for='box_vent_cols'>Vent columns</label><input type='text' id='box_vent_cols' name='box_vent_cols' value='0'></div>
        <div><label for='box_vent_hole_d'>Vent hole diameter mm</label><input type='text' id='box_vent_hole_d' name='box_vent_hole_d' value='8'></div>
      </div>
    </fieldset>
    <fieldset><legend>Decoration (etched onto the top / lid)</legend><div class='grid'>
      <div><label for='box_text'>Engraved text</label><input type='text' id='box_text' name='box_text' value=''></div>
      <div><label for='box_pattern'>Decorative pattern</label><select id='box_pattern' name='box_pattern'>
        <option value='' selected>None</option>
        <option value='diagonal'>Diagonal lines</option>
        <option value='crosshatch'>Crosshatch</option>
        <option value='hex'>Hex grid</option>
      </select></div>
      <div><label for='box_pattern_spacing'>Pattern spacing mm</label><input type='text' id='box_pattern_spacing' name='box_pattern_spacing' value='8'></div>
      <div><label for='box_logo'>Logo (SVG, straight-line shapes)</label><input type='file' id='box_logo' name='box_logo' accept='.svg'></div>
    </div></fieldset>
    <button type='submit'>Generate box kit</button>
  </form>
</div>
</div>
{recent}
<script>
const dz = document.getElementById('dz'), input = document.getElementById('step'), fl = document.getElementById('dz-file');
function updateLabel(){{ fl.textContent = input.files.length ? input.files[0].name : ''; }}
dz.addEventListener('click', () => input.click());
input.addEventListener('change', updateLabel);
['dragover'].forEach(ev => dz.addEventListener(ev, e => {{ e.preventDefault(); dz.classList.add('drag'); }}));
['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, e => {{ e.preventDefault(); dz.classList.remove('drag'); }}));
dz.addEventListener('drop', e => {{ if (e.dataTransfer.files.length) {{ input.files = e.dataTransfer.files; updateLabel(); }} }});
document.getElementById('kitform').addEventListener('submit', () => {{
  const b = document.getElementById('submitbtn'); b.disabled = true; b.textContent = 'Generating...';
}});
const tabbtns = document.querySelectorAll('.tabbtn');
tabbtns.forEach(btn => btn.addEventListener('click', () => {{
  tabbtns.forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tabpanel').forEach(p => p.hidden = true);
  btn.classList.add('active');
  document.getElementById(btn.dataset.tab).hidden = false;
}}));
</script>
"""
    return page("Slotify — STEP to laser-cut kit", body)


def job_html(job_id, status, report, log_text):
    state = status.get("state", "?")
    badge = STATE_BADGE.get(state, html.escape(state))
    head = (f"<header class='top'><div class='brand'><span class='mark'></span>"
            f"<h1>{html.escape(status.get('input', job_id))}</h1></div>{badge}</header>"
            f"<div class='sub'><a href='/'>&larr; new job</a></div>")
    if state == "running":
        cfg = status.get("config", {})
        is_box = status.get("kind") == "box"
        stages = ["generating box geometry"] if is_box else ["loading STEP"]
        if not is_box and (cfg.get("auto_shell") or cfg.get("auto_facet")):
            stages.append("preparing solids")
        stages += ["extracting plates", "resolving slab overlaps", "finding joints", "applying cuts",
                   "prismatic check", "checking collisions", "flattening", "reconstruction check"]
        if cfg.get("assembly_check", True):
            stages.append("assembly order")
        stages += ["nesting", "writing files"]
        body = head + f"""
<div class='alert ok'>Working, this page updates itself.</div>
<div class='stepline' id='stepline'>Starting...</div>
<div class='progress'><div class='progress-fill' id='pbar' style='width:4%'></div></div>
<pre class='console' id='log'>{html.escape(log_text)}</pre>
<script>
const STAGES = {json.dumps(stages)};
function updateProgress(text){{
  const done = STAGES.filter(s => text.includes(s)).length;
  const pct = Math.max(4, Math.round(100 * done / STAGES.length));
  document.getElementById('pbar').style.width = pct + '%';
  const m = [...text.matchAll(/\\[\\s*[\\d.]+s\\]\\s*(.+)/g)];
  if (m.length) document.getElementById('stepline').textContent = m[m.length - 1][1];
}}
updateProgress({json.dumps(log_text)});
async function poll(){{
  const r = await fetch('/jobs/{job_id}/status'); const s = await r.json();
  const el = document.getElementById('log'); el.textContent = s.log; el.scrollTop = el.scrollHeight;
  updateProgress(s.log);
  if (s.state !== 'running') {{ document.getElementById('pbar').style.width = '100%'; location.reload(); }}
  else setTimeout(poll, 1500);
}}
setTimeout(poll, 1500);
</script>"""
        return page("running", body)
    if state == "error":
        return page("error", head + f"<div class='alert err'><b>Failed.</b><br>{html.escape(status.get('error',''))}</div>"
                     f"<pre class='console'>{html.escape(log_text)}</pre>")
    # done
    files = report.get("files", {})

    def flink(key, text, cls=""):
        if key not in files:
            return ""
        name = os.path.basename(files[key])
        return f"<a class='{cls}' href='/jobs/{job_id}/files/{name}' download>{html.escape(text)}</a>"

    dl = "<div class='files'>" + flink("dxf", "Download DXF") + flink("step", "Download STEP of the kit", "sec") + \
        flink("report", "Report JSON", "sec") + "".join(flink(k, f"SVG sheet {k.split('_')[-1]}", "sec") for k in files if k.startswith("svg_sheet")) + "</div>"
    warns = report.get("warnings", [])
    checks = report.get("checks", {})
    asm = checks.get("assembly", {})
    rec = checks.get("reconstruction", {})
    n_joints = sum(1 for j in report.get("joints", []) if j["kind"] != "NONE")
    stats = f"""<div class='stats'>
<div class='stat'><div class='n'>{len(report.get('plates', []))}</div><div class='l'>parts</div></div>
<div class='stat'><div class='n'>{report.get('sheets', '?')}</div><div class='l'>sheet(s)</div></div>
<div class='stat'><div class='n'>{n_joints}</div><div class='l'>joints</div></div>
<div class='stat'><div class='n'>{report.get('seconds', '?')}</div><div class='l'>seconds</div></div>
<div class='stat'><div class='n'>{checks.get('max_overlap_mm3', '?')}</div><div class='l'>max overlap mm3</div></div>
</div>"""
    summary = ["3D rebuild from the flat outlines: " + ("matches" if rec.get("ok") else "<b>MISMATCH</b>")]
    if asm:
        summary.append("assembly order: " + (" &rarr; ".join(f"{html.escape(n)} ({html.escape(d)})" for n, d in asm.get("order", []))
                                             if asm.get("ok") else "not found by single-plate moves: " + html.escape(", ".join(asm.get("stuck", [])))))
    box_cls = "warn" if warns or not rec.get("ok") or (asm and not asm.get("ok")) else "ok"
    checks_html = f"<div class='alert {box_cls}'>" + "<br>".join(summary) + \
        ("<br><b>Warnings:</b><ul>" + "".join(f"<li>{html.escape(w)}</li>" for w in warns) + "</ul>" if warns else "") + "</div>"

    def fig(key, caption):
        if key not in files:
            return ""
        return (f"<figure class='prevfig'><img src='/jobs/{job_id}/files/{os.path.basename(files[key])}'>"
                f"<figcaption>{html.escape(caption)}</figcaption></figure>")

    svgs = ""
    for k in sorted(files):
        if k.startswith("svg_sheet"):
            try:
                svg = open(files[k], encoding="utf-8").read()
                svgs += f"<div class='svgwrap'>{svg}</div>"
            except OSError:
                pass
    KIND_CLASS = {"FINGER": "kind-finger", "TAB": "kind-tab", "CROSSLAP": "kind-crosslap", "NONE": "kind-none"}
    plates_rows = "".join(
        f"<tr><td>{html.escape(p['name'])}</td><td>{p['thickness']}</td><td>{p['size_mm'][0]} x {p['size_mm'][1]}</td>"
        f"<td>{p['holes']}</td><td>{html.escape(p['normal'])}</td><td>{html.escape('; '.join(p.get('notes', [])))}</td></tr>"
        for p in report.get("plates", []))
    joint_rows = "".join(
        f"<tr><td>{html.escape(j['a'])} &times; {html.escape(j['b'])}</td>"
        f"<td><span class='kind {KIND_CLASS.get(j['kind'], 'kind-none')}'>{html.escape(j['kind'])}</span></td>"
        f"<td>{j['angle_deg']}</td><td>{html.escape(j['piercer'] or '')}</td><td>{j['segments']}</td><td>{html.escape(j['note'])}</td></tr>"
        for j in report.get("joints", []))
    cfg = report.get("config", {})
    body = head + dl + stats + checks_html + f"""
<h2>Parts ({len(report.get('plates', []))})</h2><div class='preview-grid'>{fig('plates_png', 'flat parts')}</div>
<h2>Nested sheet(s) &middot; kerf {cfg.get('kerf')} mm &middot; {cfg.get('sheet_w')} x {cfg.get('sheet_h')} mm</h2>{svgs}
<h2>Assembly</h2><div class='preview-grid'>{fig('assembly_png', 'assembled')}{fig('exploded_png', 'exploded')}</div>
<h2>Plate table</h2><div class='tablewrap'><table><tr><th>part</th><th>T</th><th>size mm</th><th>holes</th><th>normal</th><th>notes</th></tr>{plates_rows}</table></div>
<h2>Interfaces</h2><div class='tablewrap'><table><tr><th>plates</th><th>joint</th><th>angle</th><th>tabs on</th><th>tabs</th><th>note</th></tr>{joint_rows}</table></div>
<details class='block'><summary>Log</summary><pre class='console'>{html.escape(log_text)}</pre></details>
"""
    return page(f"kit {status.get('input', '')}", body)


# ---------------------------------------------------------------- jobs

def job_dir(job_id):
    return os.path.join(JOBS_DIR, job_id)


def read_json(path, default=None):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def list_jobs():
    out = []
    for d in sorted(os.listdir(JOBS_DIR), key=lambda x: os.path.getmtime(os.path.join(JOBS_DIR, x)), reverse=True):
        st = read_json(os.path.join(JOBS_DIR, d, "status.json"))
        if st:
            st["id"] = d
            out.append(st)
    return out[:20]


# A job's geometry computation runs in a SEPARATE PROCESS, never in-thread inside the server. This is
# a hard safety boundary, not a style choice: on 14.09.2026 a job's uncapped boolean-geometry work grew
# to 50-100+ GB of virtual memory and blue-screened the machine twice (Windows event log, bugchecks
# 0x133/0xEF/0xD7 -- see Microsoft-Windows-Resource-Exhaustion-Detector event 2004 for the exact PIDs
# and sizes). Whatever the actual pipeline bug that let one job's memory run away, this server must
# never again let a single job's memory usage be able to take down the process serving every other job,
# let alone the OS. A per-job memory cap that kills only that job's own process enforces that regardless
# of what future geometry inputs turn out to trigger runaway growth.
JOB_MEMORY_LIMIT_MB = 4096          # a real kit build uses well under 1 GB; this is a generous, hard ceiling
SYSTEM_FREE_FLOOR_MB = 3072         # kill the job if system-wide free memory drops this low, regardless of
                                    # what the job's own process reports (covers concurrent load, other apps)
WATCHDOG_POLL_SECONDS = 1.0


def _worker_prefix():
    if FROZEN:
        # re-invoke this same exe; app_main.py's --worker branch dispatches straight into the CLI
        # instead of relaunching the server, since there is no standalone python.exe to call "-m" on
        return [sys.executable, "--worker"]
    return [sys.executable, "-u", "-m", "step2kit"]


def _cfg_common_argv(cfg, out_dir):
    a = ["--out", out_dir, "--kerf", str(cfg.kerf), "--sheet", f"{cfg.sheet_w:g}x{cfg.sheet_h:g}",
         "--gap", str(cfg.gap), "--margin", str(cfg.margin), "--min-feature", str(cfg.min_feature),
         "--facet-chord", str(cfg.facet_chord), "--facet-angle", str(cfg.facet_angle),
         "--play", str(cfg.play), "--waste-tabs", str(int(cfg.waste_tabs)), "--waste-tab-len", str(cfg.waste_tab_len)]
    if not cfg.labels:
        a.append("--no-labels")
    if not cfg.assembly_check:
        a.append("--no-assembly-check")
    return a


def cfg_to_argv(cfg, step_path, out_dir):
    a = _cfg_common_argv(cfg, out_dir)
    if cfg.thickness is not None:
        a += ["--thickness", str(cfg.thickness)]
    if cfg.finger is not None:
        a += ["--finger", str(cfg.finger)]
    if cfg.finger_min is not None:
        a += ["--finger-min", str(cfg.finger_min)]
    if cfg.wall is not None:
        a += ["--wall", str(cfg.wall)]
    if not cfg.auto_shell:
        a.append("--no-auto-shell")
    if not cfg.auto_facet:
        a.append("--no-auto-facet")
    return _worker_prefix() + [step_path] + a


def cfg_to_argv_box(cfg, w, d, h, wall, box, out_dir):
    """box: dict of the box-generator-specific options (lid, div_x, div_y, handle, vent_*, engrave_*,
    pattern*) -- kept separate from the shared Config since none of it applies to a STEP-file job."""
    a = _cfg_common_argv(cfg, out_dir)
    a += ["--box", f"{w:g}x{d:g}x{h:g}", "--wall", str(wall), "--lid", box.get("lid", "closed")]
    if box.get("div_x"):
        a += ["--div-x", str(int(box["div_x"]))]
    if box.get("div_y"):
        a += ["--div-y", str(int(box["div_y"]))]
    if box.get("handle"):
        a.append("--handle")
    if box.get("vent_face") and box.get("vent_rows") and box.get("vent_cols"):
        a += ["--vent-face", box["vent_face"], "--vent-rows", str(int(box["vent_rows"])),
             "--vent-cols", str(int(box["vent_cols"])), "--vent-hole-d", str(box.get("vent_hole_d", 8.0))]
    if box.get("text"):
        a += ["--engrave-text", box["text"]]
    if box.get("logo_path"):
        a += ["--engrave-logo", box["logo_path"]]
    if box.get("pattern"):
        a += ["--pattern", box["pattern"], "--pattern-spacing", str(box.get("pattern_spacing", 8.0))]
    return _worker_prefix() + a


def run_job(job_id, step_path, cfg):
    out_dir = os.path.join(job_dir(job_id), "out")
    _execute_job(job_id, cfg_to_argv(cfg, step_path, out_dir))


def run_box_job(job_id, w, d, h, wall, box, cfg):
    out_dir = os.path.join(job_dir(job_id), "out")
    _execute_job(job_id, cfg_to_argv_box(cfg, w, d, h, wall, box, out_dir))


def _execute_job(job_id, argv):
    import subprocess
    import psutil

    d = job_dir(job_id)
    log_path = os.path.join(d, "log.txt")
    status_path = os.path.join(d, "status.json")
    status = read_json(status_path, {})

    def log(msg):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")

    env = dict(os.environ, PYTHONUNBUFFERED="1")   # line-buffered child stdout, with or without a "-u" flag
    try:
        proc = subprocess.Popen(argv, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=env,
                                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except Exception:
        status.update(state="error", error=traceback.format_exc())
        json.dump(status, open(status_path, "w", encoding="utf-8"))
        log("ERROR: could not start the job process:\n" + traceback.format_exc())
        return

    killed_for = []

    def watchdog():
        try:
            proc_ps = psutil.Process(proc.pid)
        except psutil.NoSuchProcess:
            return
        while proc.poll() is None:
            time.sleep(WATCHDOG_POLL_SECONDS)
            try:
                mem_mb = proc_ps.memory_info().rss / (1024 * 1024)
                free_mb = psutil.virtual_memory().available / (1024 * 1024)
            except psutil.NoSuchProcess:
                return
            if mem_mb > JOB_MEMORY_LIMIT_MB:
                killed_for.append(f"job process used {mem_mb:.0f} MB, over the {JOB_MEMORY_LIMIT_MB} MB limit")
            elif free_mb < SYSTEM_FREE_FLOOR_MB:
                killed_for.append(f"system free memory dropped to {free_mb:.0f} MB (floor {SYSTEM_FREE_FLOOR_MB} MB)")
            if killed_for:
                try:
                    proc_ps.kill()
                except psutil.NoSuchProcess:
                    pass
                return

    wt = threading.Thread(target=watchdog, daemon=True)
    wt.start()

    try:
        for line in proc.stdout:
            log(line.rstrip("\n"))
    except Exception:
        pass
    proc.wait()
    wt.join(timeout=5)

    if killed_for:
        msg = f"ABORTED: {killed_for[0]}. The job was killed before it could threaten the rest of the system."
        log(msg)
        status.update(state="error", error=msg)
    elif proc.returncode == 0:
        status.update(state="done")
    else:
        status.update(state="error", error=f"job process exited with code {proc.returncode}; see the log")
    json.dump(status, open(status_path, "w", encoding="utf-8"))


def parse_multipart(handler):
    ctype = handler.headers.get("Content-Type", "")
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length)
    msg = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw, policy=email.policy.HTTP)
    fields, files = {}, {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        fname = part.get_filename()
        payload = part.get_payload(decode=True)
        if fname:
            files[name] = (fname, payload)
        else:
            fields[name] = payload.decode("utf-8", "replace").strip()
    return fields, files


def cfg_from_fields(f):
    def num(name, default):
        v = f.get(name, "").strip()
        return float(v) if v else default
    return Config(kerf=num("kerf", 0.2), thickness=num("thickness", None), finger=num("finger", None),
                  finger_min=num("finger_min", None), sheet_w=num("sheet_w", 600), sheet_h=num("sheet_h", 900),
                  gap=num("gap", 5), margin=num("margin", 10), min_feature=num("min_feature", 1.0),
                  labels="labels" in f, assembly_check="assembly" in f, wall=num("wall", None),
                  facet_chord=num("facet_chord", 0.5), facet_angle=num("facet_angle", 30.0),
                  auto_shell="auto_shell" in f, auto_facet="auto_facet" in f,
                  play=num("play", 0.0), waste_tabs=int(num("waste_tabs", 0)), waste_tab_len=num("waste_tab_len", 2.0))


class Handler(BaseHTTPRequestHandler):
    server_version = "step2kit/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_html(self, text, code=HTTPStatus.OK):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = unquote(self.path.split("?")[0])
        if path == "/":
            return self.send_html(index_html(list_jobs()))
        if path == "/kerf-test":
            import tempfile
            from step2kit.calib import kerf_test_dxf
            fd, tmp_path = tempfile.mkstemp(suffix=".dxf")
            os.close(fd)
            try:
                kerf_test_dxf(tmp_path, log=lambda *_a: None)
                data = open(tmp_path, "rb").read()
            finally:
                os.remove(tmp_path)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/dxf")
            self.send_header("Content-Disposition", 'attachment; filename="kerf_test.dxf"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "jobs":
            job_id = parts[1]
            d = job_dir(job_id)
            if not os.path.isdir(d) or "/" in job_id or ".." in job_id:
                return self.send_html(page("not found", "<h1>No such job</h1>"), HTTPStatus.NOT_FOUND)
            status = read_json(os.path.join(d, "status.json"), {"state": "?"})
            log_text = ""
            try:
                log_text = open(os.path.join(d, "log.txt"), encoding="utf-8").read()
            except OSError:
                pass
            if len(parts) == 2:
                report = read_json(os.path.join(d, "out", "kit_report.json"), {})
                return self.send_html(job_html(job_id, status, report, log_text))
            if parts[2] == "status":
                return self.send_json({"state": status.get("state"), "log": log_text[-20000:]})
            if parts[2] == "files" and len(parts) == 4:
                fname = os.path.basename(parts[3])
                fpath = os.path.join(d, "out", fname)
                if not os.path.isfile(fpath):
                    return self.send_html(page("not found", "<h1>No such file</h1>"), HTTPStatus.NOT_FOUND)
                ext = os.path.splitext(fname)[1].lower()
                data = open(fpath, "rb").read()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        self.send_html(page("not found", "<h1>Not found</h1>"), HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if self.path == "/jobs/box":
            return self._post_box()
        if self.path != "/jobs":
            return self.send_html(page("not found", "<h1>Not found</h1>"), HTTPStatus.NOT_FOUND)
        fields, files = parse_multipart(self)
        if "step" not in files or not files["step"][1]:
            return self.send_html(page("error", "<h1>No STEP file uploaded</h1><a href='/'>back</a>"), HTTPStatus.BAD_REQUEST)
        fname, payload = files["step"]
        if os.path.splitext(fname)[1].lower() not in (".step", ".stp"):
            return self.send_html(page("error", "<h1>Only .step / .stp files</h1><a href='/'>back</a>"), HTTPStatus.BAD_REQUEST)
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        d = job_dir(job_id)
        os.makedirs(os.path.join(d, "out"), exist_ok=True)
        step_path = os.path.join(d, "input" + os.path.splitext(fname)[1].lower())
        open(step_path, "wb").write(payload)
        cfg = cfg_from_fields(fields)
        status = {"state": "running", "input": os.path.basename(fname), "when": time.strftime("%d.%m.%Y %H:%M"),
                  "config": cfg.__dict__}
        json.dump(status, open(os.path.join(d, "status.json"), "w", encoding="utf-8"))
        open(os.path.join(d, "log.txt"), "w", encoding="utf-8").write("")
        threading.Thread(target=run_job, args=(job_id, step_path, cfg), daemon=True).start()
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/jobs/{job_id}")
        self.end_headers()

    def _post_box(self):
        fields, files = parse_multipart(self)

        def num(name, default):
            v = (fields.get(name) or "").strip()
            return float(v) if v else default

        w, d, h = num("box_w", 200), num("box_d", 150), num("box_h", 80)
        wall = num("box_wall", 3.0)
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        jd = job_dir(job_id)
        os.makedirs(os.path.join(jd, "out"), exist_ok=True)

        logo_path = None
        if "box_logo" in files and files["box_logo"][1]:
            fname, payload = files["box_logo"]
            if os.path.splitext(fname)[1].lower() == ".svg":
                logo_path = os.path.join(jd, "logo.svg")
                open(logo_path, "wb").write(payload)

        box = {"lid": fields.get("box_lid") or "closed", "div_x": int(num("box_div_x", 0)),
               "div_y": int(num("box_div_y", 0)), "handle": "box_handle" in fields,
               "vent_face": fields.get("box_vent_face") or None, "vent_rows": int(num("box_vent_rows", 0)),
               "vent_cols": int(num("box_vent_cols", 0)), "vent_hole_d": num("box_vent_hole_d", 8.0),
               "text": (fields.get("box_text") or "").strip() or None,
               "pattern": fields.get("box_pattern") or None, "pattern_spacing": num("box_pattern_spacing", 8.0),
               "logo_path": logo_path}
        cfg = Config(kerf=num("box_kerf", 0.2), wall=wall)
        status = {"state": "running", "input": f"box {w:g}x{d:g}x{h:g} mm ({box['lid']} lid)", "kind": "box",
                  "when": time.strftime("%d.%m.%Y %H:%M"), "config": cfg.__dict__}
        json.dump(status, open(os.path.join(jd, "status.json"), "w", encoding="utf-8"))
        open(os.path.join(jd, "log.txt"), "w", encoding="utf-8").write("")
        threading.Thread(target=run_box_job, args=(job_id, w, d, h, wall, box, cfg), daemon=True).start()
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/jobs/{job_id}")
        self.end_headers()


def start_background(host="127.0.0.1", port=4790):
    """Bind and serve in a daemon thread; returns immediately once the socket is listening. Used by the
    native-window entry point (app_main.py), which needs the server running before it can point a window
    at it. ThreadingHTTPServer's constructor binds synchronously, so by the time this returns the port is
    already accepting connections -- no polling for startup needed."""
    srv = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4790)
    ap.add_argument("--no-browser", action="store_true", help="don't auto-open a browser tab on startup")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"step2kit web UI on http://{a.host}:{a.port}  (jobs in {JOBS_DIR})")
    if not a.no_browser:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(f"http://{a.host}:{a.port}/")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
