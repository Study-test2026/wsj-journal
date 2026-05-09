"""
WSJ Journal PWA - 매일 데이터 생성
- 날짜 파라미터 지원: python generate.py 2026-05-01
- ffmpeg으로 광고 구간 실제 삭제 → 오디오/자막 완벽 싱크
- GitHub Pages에 오디오 파일도 저장
"""

import os, re, json, sys, tempfile, subprocess, requests, feedparser, anthropic
from pathlib import Path
from datetime import datetime

WSJ_RSS = "https://video-api.wsj.com/podcast/rss/wsj/the-journal"
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
EPISODES_DIR = Path("docs/episodes")
AUDIO_DIR    = Path("docs/audio")
EPISODES_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
INDEX_PATH = Path("docs/index_list.json")
TODAY_PATH = Path("docs/today.json")

AD_KEYWORDS = [
    "brought to you by","sponsor","promo code","discount",
    "advertisement","this episode is sponsored","use code",
    "free trial","click the link","download the app","percent off",
    "limited time","exclusive offer","audible","squarespace","betterhelp",
    "netsuite","indeed","shopify","wix","hubspot","masterclass",
    "dot com slash","coupon","promocode","visit us at",
    "after the break","we'll be right back","we will be right back",
    "stay with us","don't go anywhere","back in a moment",
]

def fetch_episode(target_date):
    print(f"📡 RSS 피드 가져오는 중... (목표: {target_date:%Y-%m-%d})")
    feed = feedparser.parse(WSJ_RSS)
    best, best_diff = None, None
    for e in feed.entries:
        try:
            pub = datetime(*e.get("published_parsed","")[:6])
            diff = abs((pub - target_date).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff, best = diff, e
        except: continue
    if not best: best = feed.entries[0]
    e = best
    audio_url = None
    for link in e.get("links",[]):
        if "audio" in link.get("type","") or link.get("href","").endswith(".mp3"):
            audio_url = link["href"]; break
    if not audio_url and e.get("enclosures"):
        audio_url = e["enclosures"][0].get("url")
    print(f"✅ {e.title}")
    return {"title":e.title,"description":e.get("summary",""),
            "published":e.get("published",""),"audio_url":audio_url,
            "duration":e.get("itunes_duration",""),"link":e.get("link","")}

def download_audio(url):
    print("⬇️  오디오 다운로드 중...")
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        for chunk in r.iter_content(8192): tmp.write(chunk)
    tmp.close()
    return tmp.name

def transcribe_with_timestamps(path):
    print("🎙️  Whisper 음성 인식 중...")
    import whisper
    model = whisper.load_model("small")
    result = model.transcribe(path, language="en")
    segments = [
        {"text": s["text"].strip(), "start": round(s["start"],2), "end": round(s["end"],2)}
        for s in result.get("segments",[]) if s["text"].strip()
    ]
    print(f"✅ {len(segments)}개 세그먼트")
    return result["text"], segments

def is_ad(text):
    t = text.lower()
    return sum(1 for kw in AD_KEYWORDS if kw in t) >= 1

def detect_ad_blocks(segments):
    """광고 블록 감지 → [(start, end), ...] 반환"""
    n = len(segments)
    ad_flags = [is_ad(s["text"]) for s in segments]

    # 연속 광고 구간 확장 (전후 1개씩 포함)
    expanded = [False] * n
    for i in range(n):
        if ad_flags[i]:
            for j in range(max(0,i-1), min(n,i+3)):
                if ad_flags[j]: expanded[j] = True

    # 연속된 광고 블록 묶기
    blocks = []
    i = 0
    while i < n:
        if expanded[i]:
            start = segments[i]["start"]
            while i < n and expanded[i]: i += 1
            end = segments[i-1]["end"]
            # 최소 15초 이상인 블록만 광고로 처리
            if end - start >= 15:
                blocks.append((start, end))
                print(f"🚫 광고 감지: {start:.1f}s ~ {end:.1f}s ({end-start:.1f}초)")
        else:
            i += 1
    return blocks

def remove_ad_audio(input_path, ad_blocks, date_str):
    """ffmpeg으로 광고 구간 제거 → 새 오디오 파일 반환"""
    if not ad_blocks:
        print("광고 없음 - 원본 오디오 사용")
        output_path = str(AUDIO_DIR / f"{date_str}.mp3")
        import shutil
        shutil.copy(input_path, output_path)
        return output_path

    print(f"✂️  ffmpeg으로 광고 {len(ad_blocks)}개 구간 제거 중...")

    # 유지할 구간 계산
    import subprocess
    duration_result = subprocess.run(
        ["ffprobe", "-v","error","-show_entries","format=duration",
         "-of","default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True
    )
    total_duration = float(duration_result.stdout.strip())

    # 유지할 구간 목록
    keep_segments = []
    prev_end = 0.0
    for (ad_start, ad_end) in sorted(ad_blocks):
        if prev_end < ad_start:
            keep_segments.append((prev_end, ad_start))
        prev_end = ad_end
    if prev_end < total_duration:
        keep_segments.append((prev_end, total_duration))

    # ffmpeg filter_complex로 구간 이어붙이기
    filter_parts = []
    for i, (s, e) in enumerate(keep_segments):
        filter_parts.append(f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS[a{i}]")

    concat_inputs = "".join(f"[a{i}]" for i in range(len(keep_segments)))
    filter_parts.append(f"{concat_inputs}concat=n={len(keep_segments)}:v=0:a=1[out]")
    filter_complex = ";".join(filter_parts)

    output_path = str(AUDIO_DIR / f"{date_str}.mp3")
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-codec:a", "libmp3lame", "-q:a", "3",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"⚠️  ffmpeg 오류: {result.stderr[-300:]}")
        import shutil
        shutil.copy(input_path, output_path)
        return output_path, keep_segments

    print(f"✅ 오디오 편집 완료: {output_path}")
    return output_path, keep_segments

def adjust_timestamps(segments, ad_blocks):
    """광고 제거 후 자막 타임스탬프 재조정"""
    if not ad_blocks:
        return segments

    adjusted = []
    for seg in segments:
        # 이 세그먼트가 광고 구간에 속하면 제외
        in_ad = False
        for (ad_start, ad_end) in ad_blocks:
            if seg["start"] >= ad_start and seg["end"] <= ad_end:
                in_ad = True; break
        if in_ad: continue

        # 광고 제거로 인한 시간 오프셋 계산
        offset = 0.0
        for (ad_start, ad_end) in sorted(ad_blocks):
            if ad_start < seg["start"]:
                offset += (ad_end - ad_start)

        adjusted.append({
            "text": seg["text"],
            "start": round(seg["start"] - offset, 2),
            "end":   round(seg["end"]   - offset, 2)
        })
    return adjusted

def translate_segments(segments, client):
    BATCH = 40
    all_translated = []
    for start in range(0, len(segments), BATCH):
        batch = segments[start:start+BATCH]
        texts = [s["text"] for s in batch]
        res = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=8000,
            messages=[{"role":"user","content":
                f"""아래 영어 문장 {len(texts)}개를 자연스러운 한국어로 번역하세요.
반드시 {len(texts)}개 항목을 포함한 순수 JSON 배열만 출력하세요.
형식: [{{"en":"원문","ko":"번역"}}]
입력: {json.dumps(texts, ensure_ascii=False)}"""}]
        )
        raw = re.sub(r"```json|```","",res.content[0].text).strip()
        try:
            tr = json.loads(raw)
            while len(tr) < len(batch):
                tr.append({"en":texts[len(tr)],"ko":"(번역 실패)"})
            all_translated.extend(tr[:len(batch)])
        except:
            all_translated.extend([{"en":t,"ko":"(번역 실패)"} for t in texts])
        print(f"  번역 {start+len(batch)}/{len(segments)}")
    return all_translated

def analyze(episode, transcript, client):
    res = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=2000,
        messages=[{"role":"user","content":
            f"""WSJ The Journal 팟캐스트 분석:
제목: {episode['title']}
설명: {episode['description']}
스크립트: {transcript[:4000]}

순수 JSON만 출력:
{{"summary_ko":"5-7문장 요약","key_points":["포인트1","포인트2","포인트3"],
"related_stocks":[{{"ticker":"AAPL","name":"Apple","reason":"이유","sentiment":"positive","type":"stock"}}],
"related_etfs":[{{"ticker":"XLK","name":"Tech ETF","reason":"이유","sentiment":"neutral","type":"etf"}}],
"market_outlook":"2-3문장 시장 전망"}}"""}]
    )
    raw = re.sub(r"```json|```","",res.content[0].text).strip()
    try:
        data = json.loads(raw)
        data["related_stocks"] = data.get("related_stocks",[]) + data.get("related_etfs",[])
        return data
    except:
        return {"summary_ko":"분석 실패","key_points":[],"related_stocks":[],"market_outlook":""}

def update_index(date_str, episode, audio_filename):
    try:
        index = json.loads(INDEX_PATH.read_text()) if INDEX_PATH.exists() else []
    except:
        index = []
    index = [e for e in index if e["date"] != date_str]
    index.insert(0, {
        "date": date_str,
        "title": episode["title"],
        "published": episode["published"],
        "duration": episode["duration"],
        "file": f"episodes/{date_str}.json"
    })
    if len(index) > 5:
        for old in index[5:]:
            Path("docs") / old["file"] and (Path("docs") / old["file"]).unlink(missing_ok=True)
            print(f"🗑️  삭제: {old['file']}")
    index = index[:5]
    index.sort(key=lambda x: x["date"], reverse=True)
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=2))
    print(f"📋 index_list.json: {len(index)}개")

def main():
    # 날짜 파라미터
    if len(sys.argv) > 1 and sys.argv[1]:
        date_str = sys.argv[1]
    elif os.environ.get("TARGET_DATE"):
        date_str = os.environ["TARGET_DATE"]
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")
    print(f"📅 처리 날짜: {date_str}")

    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        print(f"❌ 날짜 형식 오류: {date_str}")
        sys.exit(1)

    out_path = EPISODES_DIR / f"{date_str}.json"
    if out_path.exists():
        print(f"⏭️  이미 존재: {date_str} - 스킵")
        return

    print("="*50)
    print(f"🗞️  WSJ PWA 생성 — {datetime.now():%Y-%m-%d %H:%M}")
    print("="*50)

    ep = fetch_episode(target_date)
    raw_audio = download_audio(ep["audio_url"])

    try:
        # 1. Whisper 전사
        transcript, segments = transcribe_with_timestamps(raw_audio)

        # 2. 광고 블록 감지
        ad_blocks = detect_ad_blocks(segments)
        print(f"총 {len(ad_blocks)}개 광고 블록 감지")

        # 3. 오디오에서 광고 제거 (ffmpeg)
        if ad_blocks:
            clean_audio, keep_segs = remove_ad_audio(raw_audio, ad_blocks, date_str)
            # 4. 자막 타임스탬프 재조정
            clean_segments = adjust_timestamps(segments, ad_blocks)
        else:
            import shutil
            clean_audio = str(AUDIO_DIR / f"{date_str}.mp3")
            shutil.copy(raw_audio, clean_audio)
            clean_segments = segments

        # 5. GitHub Pages URL로 오디오 경로 설정
        github_user = "Study-test2026"  # ← 본인 GitHub 사용자명
        repo_name = "wsj-journal"
        audio_url = f"https://{github_user.lower()}.github.io/{repo_name}/audio/{date_str}.mp3"

        # 6. Claude 번역
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        translations = translate_segments(clean_segments, client)

        subtitles = []
        for i, seg in enumerate(clean_segments):
            ko = translations[i]["ko"] if i < len(translations) else "(번역 실패)"
            subtitles.append({"en":seg["text"],"ko":ko,"start":seg["start"],"end":seg["end"]})

        # 7. 분석
        print("🤖 분석 중...")
        clean_transcript = " ".join(s["text"] for s in clean_segments)
        analysis = analyze(ep, clean_transcript, client)

        # 8. 저장
        out = {
            **ep,
            "date": date_str,
            "audio_url": audio_url,  # 편집된 오디오 URL
            "original_audio_url": ep["audio_url"],  # 원본 URL 백업
            "subtitles": subtitles,
            "analysis": analysis,
            "ad_blocks_removed": len(ad_blocks),
            "generated_at": datetime.now().isoformat()
        }
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))

        # today.json 업데이트
        try:
            index = json.loads(INDEX_PATH.read_text()) if INDEX_PATH.exists() else []
        except:
            index = []
        latest = index[0]["date"] if index else ""
        if date_str >= latest:
            TODAY_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
            print("📌 today.json 업데이트")

        update_index(date_str, ep, f"{date_str}.mp3")
        print(f"\n✅ 완료: {out_path}")
        print(f"   자막: {len(subtitles)}개 | 광고 제거: {len(ad_blocks)}블록")

    finally:
        Path(raw_audio).unlink(missing_ok=True)

if __name__ == "__main__":
    main()
