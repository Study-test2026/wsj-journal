"""
WSJ Journal PWA - 매일 데이터 생성
- 미국 동부시간(ET) 기준으로 날짜 처리
- ffmpeg으로 광고 구간 실제 삭제 (보수적 감지)
- GitHub Pages에 오디오 파일도 저장
"""

import os, re, json, sys, tempfile, subprocess, requests, feedparser, anthropic
from pathlib import Path
from datetime import datetime, timedelta, timezone

WSJ_RSS = "https://video-api.wsj.com/podcast/rss/wsj/the-journal"
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
EPISODES_DIR = Path("docs/episodes")
AUDIO_DIR    = Path("docs/audio")
EPISODES_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
INDEX_PATH = Path("docs/index_list.json")
TODAY_PATH = Path("docs/today.json")

# ── 광고 판별 키워드 (보수적으로 - 명확한 광고 표현만) ──
# 강한 신호: 1개만 있어도 광고
STRONG_AD_KEYWORDS = [
    "this episode is brought to you by",
    "this podcast is brought to you by",
    "this episode is sponsored by",
    "promo code",
    "use the code",
    "use code",
    "sign up at",
    "go to ",
    "visit ",
    "for a free trial",
    "free trial at",
    "percent off your",
    "% off your",
    "limited time offer",
]
# 약한 신호: 2개 이상 함께 있어야 광고
WEAK_AD_KEYWORDS = [
    "audible","squarespace","betterhelp","netsuite","indeed",
    "shopify","wix","hubspot","masterclass","stamps.com",
    "linkedin learning","peloton","mint mobile","zip recruiter",
    "discount","coupon","subscription",
]

def fetch_episode(target_date):
    print(f"📡 RSS 피드 가져오는 중... (목표 ET 날짜: {target_date:%Y-%m-%d})")
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
    print(f"✅ 매칭된 에피소드: {e.title}")
    print(f"   발행일: {e.get('published','')}")
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
    print("🎙️  Whisper 음성 인식 중 (small 모델)...")
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
    """광고 판별: 강한 키워드 1개 또는 약한 키워드 2개 이상"""
    t = text.lower()
    strong = sum(1 for kw in STRONG_AD_KEYWORDS if kw in t)
    if strong >= 1:
        return True
    weak = sum(1 for kw in WEAK_AD_KEYWORDS if kw in t)
    return weak >= 2

def detect_ad_blocks(segments):
    """광고 블록 감지 - 보수적으로 (확실한 광고만)"""
    n = len(segments)
    ad_flags = [is_ad(s["text"]) for s in segments]

    # 광고 신호 주변 ±2개 세그먼트만 포함 (좁게)
    expanded = [False] * n
    for i in range(n):
        if ad_flags[i]:
            for j in range(max(0,i-2), min(n,i+3)):
                expanded[j] = True

    # 연속된 광고 블록 묶기
    blocks = []
    i = 0
    while i < n:
        if expanded[i]:
            start_idx = i
            while i < n and expanded[i]: i += 1
            end_idx = i - 1
            block_start = segments[start_idx]["start"]
            block_end   = segments[end_idx]["end"]
            duration    = block_end - block_start

            # 30초 이상 + 강한 광고 신호 1개 이상이어야 광고로 인정
            has_strong = any(
                any(kw in segments[k]["text"].lower() for kw in STRONG_AD_KEYWORDS)
                for k in range(start_idx, end_idx+1)
            )
            if duration >= 30 and has_strong:
                blocks.append((block_start, block_end))
                print(f"🚫 광고 감지: {block_start:.1f}s ~ {block_end:.1f}s ({duration:.1f}초)")
            else:
                print(f"⏭️  광고 의심 구간 무시: {block_start:.1f}s ({duration:.1f}초, 강한신호={has_strong})")
        else:
            i += 1
    return blocks

def remove_ad_audio(input_path, ad_blocks, date_str):
    """ffmpeg으로 광고 구간 제거"""
    output_path = str(AUDIO_DIR / f"{date_str}.mp3")
    if not ad_blocks:
        print("광고 없음 - 원본 오디오 그대로 사용")
        import shutil
        shutil.copy(input_path, output_path)
        return output_path

    print(f"✂️  ffmpeg으로 광고 {len(ad_blocks)}개 구간 제거 중...")

    duration_result = subprocess.run(
        ["ffprobe","-v","error","-show_entries","format=duration",
         "-of","default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True
    )
    total_duration = float(duration_result.stdout.strip())

    keep_segments = []
    prev_end = 0.0
    for (ad_start, ad_end) in sorted(ad_blocks):
        if prev_end < ad_start:
            keep_segments.append((prev_end, ad_start))
        prev_end = ad_end
    if prev_end < total_duration:
        keep_segments.append((prev_end, total_duration))

    filter_parts = []
    for i, (s, e) in enumerate(keep_segments):
        filter_parts.append(f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS[a{i}]")
    concat_inputs = "".join(f"[a{i}]" for i in range(len(keep_segments)))
    filter_parts.append(f"{concat_inputs}concat=n={len(keep_segments)}:v=0:a=1[out]")
    filter_complex = ";".join(filter_parts)

    cmd = ["ffmpeg","-y","-i",input_path,
           "-filter_complex",filter_complex,
           "-map","[out]",
           "-codec:a","libmp3lame","-q:a","3",
           output_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"⚠️  ffmpeg 오류: {result.stderr[-300:]}")
        import shutil
        shutil.copy(input_path, output_path)
        return output_path

    print(f"✅ 오디오 편집 완료")
    return output_path

def adjust_timestamps(segments, ad_blocks):
    """광고 제거 후 자막 타임스탬프 재조정"""
    if not ad_blocks:
        return segments

    adjusted = []
    for seg in segments:
        # 광고 구간에 속하는 세그먼트는 제외 (중심점 기준)
        center = (seg["start"] + seg["end"]) / 2
        in_ad = any(ad_s <= center <= ad_e for (ad_s, ad_e) in ad_blocks)
        if in_ad: continue

        # 이 세그먼트 시작 이전의 광고 길이 합산
        offset = sum(
            (ad_e - ad_s) for (ad_s, ad_e) in sorted(ad_blocks)
            if ad_e <= seg["start"]
        )

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

def update_index(date_str, episode):
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
            old_json = Path("docs") / old["file"]
            old_audio = Path("docs/audio") / f"{old['date']}.mp3"
            old_json.unlink(missing_ok=True)
            old_audio.unlink(missing_ok=True)
            print(f"🗑️  삭제: {old['file']} + audio")
    index = index[:5]
    index.sort(key=lambda x: x["date"], reverse=True)
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=2))
    print(f"📋 index_list.json: {len(index)}개")

def get_target_date_string():
    """ET(뉴욕) 시간 기준 오늘 날짜 반환"""
    if len(sys.argv) > 1 and sys.argv[1]:
        return sys.argv[1]
    if os.environ.get("TARGET_DATE"):
        return os.environ["TARGET_DATE"]

    # ET 기준 현재 시간 (UTC-4 EDT 가정, 겨울에는 UTC-5)
    # 더 정확하게는 zoneinfo 사용
    try:
        from zoneinfo import ZoneInfo
        et_now = datetime.now(ZoneInfo("America/New_York"))
    except ImportError:
        # 폴백: UTC-4 (EDT) 사용
        et_now = datetime.now(timezone.utc) - timedelta(hours=4)

    return et_now.strftime("%Y-%m-%d")

def main():
    date_str = get_target_date_string()
    print(f"📅 처리 날짜 (ET 기준): {date_str}")

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

        # 2. 광고 블록 감지 (보수적)
        ad_blocks = detect_ad_blocks(segments)
        print(f"총 {len(ad_blocks)}개 광고 블록 확정")

        # 3. 오디오에서 광고 제거
        clean_audio = remove_ad_audio(raw_audio, ad_blocks, date_str)

        # 4. 자막 타임스탬프 재조정
        clean_segments = adjust_timestamps(segments, ad_blocks)

        # 5. GitHub Pages URL
        github_user = "study-test2026"
        repo_name = "wsj-journal"
        audio_url = f"https://{github_user}.github.io/{repo_name}/audio/{date_str}.mp3"

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
            "audio_url": audio_url,
            "original_audio_url": ep["audio_url"],
            "subtitles": subtitles,
            "analysis": analysis,
            "ad_blocks_removed": len(ad_blocks),
            "generated_at": datetime.now().isoformat()
        }
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))

        # today.json
        try:
            index = json.loads(INDEX_PATH.read_text()) if INDEX_PATH.exists() else []
        except:
            index = []
        latest = index[0]["date"] if index else ""
        if date_str >= latest:
            TODAY_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
            print("📌 today.json 업데이트")

        update_index(date_str, ep)
        print(f"\n✅ 완료: {out_path}")
        print(f"   자막: {len(subtitles)}개 | 광고 제거: {len(ad_blocks)}블록")

    finally:
        Path(raw_audio).unlink(missing_ok=True)

if __name__ == "__main__":
    main()
