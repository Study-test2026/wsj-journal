"""최근 10개만 유지 + index.json 갱신"""
import json
from pathlib import Path

root = Path("data/episodes")
dirs = sorted([d for d in root.iterdir()
               if d.is_dir() and d.name != "latest"], reverse=True)

# 11번째부터 삭제
for d in dirs[10:]:
    for f in d.iterdir(): f.unlink()
    d.rmdir()

# index 갱신
idx = []
for d in dirs[:10]:
    meta = json.loads((d / "sync.json").read_text())["episode"]
    idx.append({
        "date": d.name,
        "title": meta.get("title", ""),
        "summary_ko": meta.get("summary_ko", "")[:120],
    })
(root / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=2))

# latest 심볼릭
latest = root / "latest"
if latest.exists() or latest.is_symlink(): 
    latest.unlink()
latest.symlink_to(dirs[0].name)
print(f"✓ {len(idx)} episodes")