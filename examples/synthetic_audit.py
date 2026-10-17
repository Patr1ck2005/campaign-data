"""Generate tiny CSVs, audit through the public API, and draw stdlib-only SVGs.

No plotting dependency or external dataset is required. Output is confined to --out.
"""
from __future__ import annotations
import argparse
import contextlib
import csv
import hashlib
import html
import importlib.metadata
import io
import json
from pathlib import Path
from campaign_data import audit_directory, format_audit_report, peek_table, smart_merge

BLUE,RED,TEAL,GRAY = "#1257df","#e31a35","#009b83","#dde4ed"


def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def text(x,y,value,size=17,fill="#172238"):
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}">{html.escape(str(value))}</text>'


def rect(x,y,w,h,color):return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="3" fill="{color}"/>'


def svg(body,width=1000,height=430):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="white"/><g font-family="Arial,sans-serif">{body}</g></svg>'


def main(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    inputs=out/"inputs";inputs.mkdir(exist_ok=True)
    definitions={
        "coarse-a.csv": [(x,y,10+x+.2*y) for x in (0,1,2) for y in (0,1,2) if (x,y)!=(1,1)],
        "coarse-b.csv": [(x,y,10+x+.2*y) for x in (1,2,3) for y in (0,1,2)],
        "fine-c.csv": [(x,y,10+x+.2*y) for x in (1,1.02,1.04) for y in (0,1)],
    }
    definitions["coarse-b.csv"].append(definitions["coarse-b.csv"][0])
    for name,rows in definitions.items():
        (inputs/name).write_text("x,y,value\n"+"".join(f"{x:g},{y:g},{v:g}\n" for x,y,v in rows),encoding="utf-8")
    before={name:sha(inputs/name) for name in definitions}
    capture=io.StringIO()
    with contextlib.redirect_stdout(capture):
        report=audit_directory(inputs,header_len=2,use_cache=False)
    assert report.file_count==3
    assert report.overlaps,"Expected overlap was not reported"
    assert report.conflicts,"Expected mesh-mismatch conflict was not reported"
    assert any(f.completeness<1 for f in report.files)
    # Merge only compatible coarse meshes. The conflicting fine mesh stays separate.
    with contextlib.redirect_stdout(capture):
        smart_merge([inputs/"coarse-a.csv",inputs/"coarse-b.csv"],out_path=out/"merged-coarse.tsv",header_len=2,use_cache=False)
    with (out/"merged-coarse.tsv").open(encoding="utf-8-sig",newline="") as stream:
        merged=list(csv.DictReader(stream,delimiter="\t"))
    assert len(merged)==12
    assert len({(float(row["x"]),float(row["y"])) for row in merged})==12
    after={name:sha(inputs/name) for name in definitions}
    assert before==after,"Input bytes changed"
    formatted=format_audit_report(report).replace(str(inputs),"inputs")
    (out/"audit-report.txt").write_text(formatted+"\n",encoding="utf-8")
    (out/"peek.txt").write_text(peek_table(inputs/"coarse-a.csv").replace(str(inputs),"inputs")+"\n",encoding="utf-8")
    # Retain actual CLI/library output, with workstation paths removed.
    log=capture.getvalue().replace(str(out),".").replace(str(out.resolve()),".")
    (out/"execution.txt").write_text(log,encoding="utf-8")
    items=[]
    def save(stem,title,caption,body,width=1000,height=430):
        (out/f"{stem}.svg").write_text(svg(body,width,height),encoding="utf-8")
        items.append({"stem":stem,"title":title,"caption":caption,"sha256":sha(out/f"{stem}.svg"),"pixels":[width,height]})
    body=text(35,42,"CSV files → mesh fingerprints → auditable decisions",25)
    cards=[("INPUT FILES",report.file_count,BLUE),("ROWS",report.total_rows,TEAL),("OVERLAP PAIRS",len(report.overlaps),BLUE),("MESH CONFLICTS",len(report.conflicts),RED)]
    for i,(label,value,color) in enumerate(cards):
        xx=35+i*241;body+=rect(xx,75,220,115,"#f2f6fb")+text(xx+16,103,label,13)+text(xx+16,158,value,40,color)
    body+=text(35,235,"Source bytes are unchanged",23,TEAL)
    body+=text(35,272,"SHA-256 checked before and after audit + explicit-output merge.")
    body+=text(35,318,"Fine/coarse mesh conflicts stay visible; only compatible coarse files are merged.")
    body+=text(35,370,"Synthetic inputs: z = 10 + x + 0.2y; one hole, one repeated row, two mesh steps.",16)
    save("hero","Audit before merge","Counts come directly from AuditReport. A mesh conflict is not proof of a contradictory measured value.",body)
    body=text(35,40,"Coverage by file and anchor parameter",24)
    entries=report.coverage;cell=min(100,680/max(len(entries),1));left=240;top=100
    for j,e in enumerate(entries):
        body+=text(left+j*cell,78,f"{e.param_value:g}",14)
    for i,f in enumerate(report.files):
        yy=top+i*64;body+=text(35,yy+31,f.name,17)
        for j,e in enumerate(entries):
            color=RED if e.is_conflict and f.name in e.file_names else BLUE if f.name in e.file_names else GRAY
            body+=rect(left+j*cell,yy,cell-7,46,color)
    body+=text(35,340,f"Anchor column selected by audit: {report.anchor_col_name}",18)
    body+=text(35,375,"Blue: present   Gray: absent   Red: present in a cross-campaign mesh conflict",16)
    save("coverage","Coverage and conflicts","This is per-anchor-value coverage from the library, not a voxel completeness map. Red denotes different mesh fingerprints sharing an anchor value.",body)
    body=text(35,40,"Completeness uses each file's inferred parameter product",24)
    for i,f in enumerate(report.files):
        yy=87+i*85;body+=text(35,yy+23,f.name,18)+rect(240,yy,500,32,GRAY)+rect(240,yy,500*f.completeness,32,TEAL)
        body+=text(758,yy+24,f"{f.actual_points}/{f.expected_points}",18)+text(240,yy+58,f"rows {f.data_rows}; duplicate rows {f.duplicate_rows}",14)
    body+=text(35,385,"Completeness is relative to inferred per-file axes; it does not know an external intended scan.",16)
    save("completeness","Missing points and duplicate rows","The deliberately missing (1,1) site leaves coarse-a at 8/9. The repeated coarse-b row is a separate issue from grid completeness.",body)
    names=[f.name for f in report.files];n=len(names);counts=[[0]*n for _ in range(n)]
    for pair in report.overlaps:
        i,j=names.index(pair.file_a_name),names.index(pair.file_b_name);counts[i][j]=counts[j][i]=pair.n_shared_points
    body=text(35,40,"Shared grid points across files",24)
    for i,name in enumerate(names):
        body+=text(240+i*160,82,name,15)+text(35,132+i*70,name,17)
        for j in range(n):
            value="—" if i==j else str(counts[i][j]);body+=rect(240+j*160,100+i*70,145,55,"#e8effb" if i==j else "#c8daf7")+text(302+j*160,135+i*70,value,22,BLUE)
    body+=text(35,365,"Overlap measures shared parameter tuples. It does not choose the scientific identity of a mode.",16)
    save("overlap","Pairwise overlap","Each off-diagonal count is taken from an OverlapPair in the audit result. Duplicate resolution is demonstrated only on compatible coarse files.",body)
    manifest={"evidence_type":"computed synthetic file-audit demonstration","data_origin":"independent z=10+x+0.2y CSV definitions","seed":None,"source":"../../../examples/synthetic_audit.py","source_sha256":sha(__file__),"generation_command":"python examples/synthetic_audit.py --out docs/assets/showcase","versions":{"campaign-data":importlib.metadata.version("campaign-data")},"input_sha256_before":before,"input_sha256_after":after,"checks":{"inputs_unchanged":before==after,"file_count":report.file_count,"rows":report.total_rows,"overlap_pairs":len(report.overlaps),"mesh_conflicts":len(report.conflicts),"merged_unique_points":len(merged),"all_assertions_passed":True},"images":items}
    (out/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
    page='<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Campaign Data · synthetic audit</title><style>body{max-width:1100px;margin:36px auto;padding:0 22px;font:16px/1.6 system-ui;color:#172238;background:#f6f8fc}section{background:white;padding:20px;margin:24px 0;border:1px solid #dbe2ed;border-radius:12px}img{width:100%;height:auto}a{color:#1257df}</style><h1>Campaign Data · synthetic audit</h1><p>All data are generated by the runnable example. Blue means present; gray absent; red means overlapping mesh campaigns. Completeness is relative to each file’s inferred parameter product. No experiment, solver or unpublished input is used.</p><p><a href="manifest.json">Hashes and provenance</a> · <a href="audit-report.txt">Actual audit report</a> · <a href="../../../examples/synthetic_audit.py">Runnable source</a> · <a href="../../../README.md">README</a></p>'
    for item in items:page+=f'<section><h2>{html.escape(item["title"])}</h2><img src="{item["stem"]}.svg" alt="{html.escape(item["caption"])}"><p>{html.escape(item["caption"])}</p><a href="{item["stem"]}.svg">SVG</a></section>'
    (out/"gallery.html").write_text(page+"</html>",encoding="utf-8")
    print(json.dumps(manifest["checks"]))


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--out",type=Path,default=Path("docs/assets/showcase"));main(p.parse_args().out)
