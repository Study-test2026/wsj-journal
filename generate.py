"""
WSJ Journal PWA - 매일 데이터 생성
- 날짜 파라미터 지원: python generate.py 2026-05-01
- 기본값: 오늘 날짜 (자동 실행 시)
- RSS에서 해당 날짜에 가장 가까운 에피소드 자동 매칭
"""

import os, re, json, sys, tempfile, requests, feedparser, anthropic
from pathlib import Path
from datetime import datetime, timedelta

WSJ_RSS = "https://video-api.wsj.com/podcast/rss/wsj/the-journal"
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
EPISODES_DIR = Path("docs/episodes")
EPISODES_DIR.mkdir(parents=True, exist_ok=True)
INDEX_PATH = Path("docs/index_list.json")
TODAY_PATH = Path("docs/today.json")


def fetch_episode(target_date: datetime):
    """RSS에서 target_date에 가장 가까운 에피소드 가져오기"""
    print(f"📡 RSS 피드 가져오는 중... (목표 날짜: {target_date.strftime('%Y-%m-%d')})")
    feed = feedparser.parse(WSJ_RSS)

    best_entry = None
    best_diff = None

    for entry in feed.entries:
        try:
            pub = entry.get("published_parsed")
            if not pub:
                continue
            pub_dt = datetime(*pub[:6])
            diff = abs((pub_dt - target_date).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_entry = entry
        except:
            continue

    if not best_entry:
        best_entry = feed.entries[0]

    e = best_entry
    audio_url = None
    for link in e.get("links", []):
        if "audio" in link.get("type", "") or link.get("href", "").endswith(".mp3"):
            audio_url = link["href"]; break
    if not audio_url and e.get("enclosures"):
        audio_url = e["enclosures"][0].get("url")

    print(f"✅ {e.title}")
    return {
        "title": e.title,
        "description": e.get("summary", ""),
        "published": e.get("published", ""),
        "audio_url": audio_url,
        "duration": e.get("itunes_duration", ""),
        "link": e.get("link", ""),
    }


def download_audio(url):
    print("⬇️  오디오 다운로드 중...")
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        for chunk in r.iter_content(8192): tmp.write(chunk)
    tmp.close()
    return tmp.name


def transcribe(path):
    print("🎙️  Whisper 음성 인식 중...")
    import whisper
    model = whisper.load_model("small")
    result = model.transcribe(path, language="en")
    segments = [
        {"text": s["text"].strip(), "start": round(s["start"], 2), "end": round(s["end"], 2)}
        for s in result.get("segments", []) if s["text"].strip()
    ]
    print(f"✅ {len(segments)}개 세그먼트")
    return result["text"], segments


def translate_segments(segments, client):
    BATCH = 40
    all_translated = []
    for start in range(0, len(segments), BATCH):
        batch = segments[start:start+BATCH]
        texts = [s["text"] for s in batch]
        res = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=8000,
            messages=[{"role": "user", "content":
                f"""아래 영어 문장 {len(texts)}개를 자연스러운 한국어로 번역하세요.
반드시 {len(texts)}개 항목을 포함한 순수 JSON 배열만 출력하세요.
형식: [{{"en":"원문","ko":"번역"}}]
입력: {json.dumps(texts, ensure_ascii=False)}"""}]
        )
        raw = re.sub(r"```json|```", "", res.content[0].text).strip()
        try:
            tr = json.loads(raw)
            while len(tr) < len(batch):
                tr.append({"en": texts[len(tr)], "ko": "(번역 실패)"})
            all_translated.extend(tr[:len(batch)])
        except:
            all_translated.extend([{"en": t, "ko": "(번역 실패)"} for t in texts])
        print(f"  번역 {start+len(batch)}/{len(segments)}")
    return all_translated


def analyze(episode, transcript, client):
    res = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=2000,
        messages=[{"role": "user", "content":
            f"""WSJ The Journal 팟캐스트 분석:
제목: {episode['title']}
설명: {episode['description']}
스크립트: {transcript[:4000]}

순수 JSON만 출력 (마크다운 없이):
{{"summary_ko":"5-7문장 요약","key_points":["포인트1","포인트2","포인트3"],
"related_stocks":[{{"ticker":"AAPL","name":"Apple Inc.","reason":"이유","sentiment":"positive","type":"stock"}}],
"related_etfs":[{{"ticker":"XLK","name":"Tech ETF","reason":"이유","sentiment":"neutral","type":"etf"}}],
"market_outlook":"2-3문장 시장 전망"}}
관련주 3-5개, ETF 2-3개."""}]
    )
    raw = re.sub(r"```json|```", "", res.content[0].text).strip()
    try:
        data = json.loads(raw)
        data["related_stocks"] = data.get("related_stocks", []) + data.get("related_etfs", [])
        return data
    except:
        return {"summary_ko": "분석 실패", "key_points": [], "related_stocks": [], "market_outlook": ""}


def update_index(date_str, episode):
    """index_list.json 업데이트 (최근 5개 유지)"""
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

    # 5개 초과분 파일 삭제
    if len(index) > 5:
        for old in index[5:]:
            old_file = Path("docs") / old["file"]
            old_file.unlink(missing_ok=True)
            print(f"🗑️  삭제: {old['file']}")
    index = index[:5]

    # 날짜 기준 정렬 (최신순)
    index.sort(key=lambda x: x["date"], reverse=True)
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=2))
    print(f"📋 index_list.json: {len(index)}개 에피소드")


def main():
    # 날짜 파라미터 처리
    # 우선순위: 1) 커맨드라인 인자, 2) 환경변수, 3) 오늘 날짜
    if len(sys.argv) > 1:
        date_str = sys.argv[1]
        print(f"📅 지정 날짜 모드: {date_str}")
    elif os.environ.get("TARGET_DATE"):
        date_str = os.environ.get("TARGET_DATE")
        print(f"📅 환경변수 날짜 모드: {date_str}")
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")
        print(f"📅 오늘 날짜 모드: {date_str}")

    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        print(f"❌ 날짜 형식 오류: {date_str} (YYYY-MM-DD 형식으로 입력하세요)")
        sys.exit(1)

    out_path = EPISODES_DIR / f"{date_str}.json"

    # 이미 처리된 날짜면 스킵
    if out_path.exists():
        print(f"⏭️  {date_str} 에피소드가 이미 존재합니다. 스킵합니다.")
        print("   덮어쓰려면 파일을 먼저 삭제하세요.")
        return

    print("=" * 50)
    print(f"🗞️  WSJ PWA 데이터 생성 — {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 50)

    ep = fetch_episode(target_date)
    audio_path = download_audio(ep["audio_url"])

    try:
        transcript, segments = transcribe(audio_path)
        clean_segs = segments
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        translations = translate_segments(clean_segs, client)

        subtitles = []
        for i, seg in enumerate(clean_segs):
            ko = translations[i]["ko"] if i < len(translations) else "(번역 실패)"
            subtitles.append({"en": seg["text"], "ko": ko, "start": seg["start"], "end": seg["end"]})

        print("🤖 에피소드 분석 중...")
        full_transcript = " ".join(s["text"] for s in clean_segs)
        analysis = analyze(ep, full_transcript, client)

        out = {
            **ep,
            "date": date_str,
            "subtitles": subtitles,
            "analysis": analysis,
            "ad_segments_removed": 0,
            "generated_at": datetime.now().isoformat()
        }

        # 날짜별 저장
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))

        # today.json = 가장 최신 날짜 에피소드로 업데이트
        try:
            index = json.loads(INDEX_PATH.read_text()) if INDEX_PATH.exists() else []
        except:
            index = []

        # 현재 처리한 날짜가 가장 최신이면 today.json 업데이트
        latest_date = index[0]["date"] if index else ""
        if date_str >= latest_date:
            TODAY_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
            print("📌 today.json 업데이트됨")

        update_index(date_str, ep)

        print(f"\n✅ 저장 완료: {out_path}")
        print(f"   자막: {len(subtitles)}개")

    finally:
        Path(audio_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
