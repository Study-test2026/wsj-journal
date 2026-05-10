"""
WSJ Journal PWA - 매일 데이터 생성
- 미국 동부시간(ET) 기준으로 날짜 처리
- 광고 포함 전체 오디오 그대로 유지 (싱크 완벽 보장)
- WSJ 원본 오디오 URL 직접 사용 (저장 공간 절약)
"""

import os, re, json, sys, tempfile, requests, feedparser, anthropic
from pathlib import Path
from datetime import datetime, timedelta, timezone

WSJ_RSS = "https://video-api.wsj.com/podcast/rss/wsj/the-journal"
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
EPISODES_DIR = Path("docs/episodes")
EPISODES_DIR.mkdir(parents=True, exist_ok=True)
INDEX_PATH = Path("docs/index_list.json")
TODAY_PATH = Path("docs/today.json")


def fetch_episode(target_date):
    print(f"📡 RSS 피드 가져오는 중... (목표 ET 날짜: {target_date:%Y-%m-%d})")
    feed = feedparser.parse(WSJ_RSS)
    best, best_diff = None, None
    for e in feed.entries:
        try:
            pub = datetime(*e.get("published_parsed", "")[:6])
            diff = abs((pub - target_date).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff, best = diff, e
        except:
            continue
    if not best:
        best = feed.entries[0]
    e = best
    audio_url = None
    for link in e.get("links", []):
        if "audio" in link.get("type", "") or link.get("href", "").endswith(".mp3"):
            audio_url = link["href"]; break
    if not audio_url and e.get("enclosures"):
        audio_url = e["enclosures"][0].get("url")
    print(f"✅ 매칭된 에피소드: {e.title}")
    print(f"   발행일: {e.get('published', '')}")
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


def transcribe_with_timestamps(path):
    print("🎙️  Whisper 음성 인식 중 (small 모델)...")
    import whisper
    model = whisper.load_model("small")
    result = model.transcribe(path, language="en")
    segments = [
        {"text": s["text"].strip(), "start": round(s["start"], 2), "end": round(s["end"], 2)}
        for s in result.get("segments", []) if s["text"].strip()
    ]
    print(f"✅ {len(segments)}개 세그먼트 전사 완료")
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

순수 JSON만 출력 (광고 부분은 무시하고 본문 내용만 분석):
{{"summary_ko":"5-7문장 요약","key_points":["포인트1","포인트2","포인트3"],
"related_stocks":[{{"ticker":"AAPL","name":"Apple","reason":"이유","sentiment":"positive","type":"stock"}}],
"related_etfs":[{{"ticker":"XLK","name":"Tech ETF","reason":"이유","sentiment":"neutral","type":"etf"}}],
"market_outlook":"2-3문장 시장 전망"}}"""}]
    )
    raw = re.sub(r"```json|```", "", res.content[0].text).strip()
    try:
        data = json.loads(raw)
        data["related_stocks"] = data.get("related_stocks", []) + data.get("related_etfs", [])
        return data
    except:
        return {"summary_ko": "분석 실패", "key_points": [], "related_stocks": [], "market_outlook": ""}


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
            (Path("docs") / old["file"]).unlink(missing_ok=True)
            print(f"🗑️  삭제: {old['file']}")
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

    try:
        from zoneinfo import ZoneInfo
        et_now = datetime.now(ZoneInfo("America/New_York"))
    except ImportError:
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

    print("=" * 50)
    print(f"🗞️  WSJ PWA 생성 — {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 50)

    ep = fetch_episode(target_date)
    raw_audio = download_audio(ep["audio_url"])

    try:
        # 1. Whisper 전사 (광고 포함)
        transcript, segments = transcribe_with_timestamps(raw_audio)

        # 2. Claude 번역
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        translations = translate_segments(segments, client)

        # 3. 자막 생성 (광고 포함 전체)
        subtitles = []
        for i, seg in enumerate(segments):
            ko = translations[i]["ko"] if i < len(translations) else "(번역 실패)"
            subtitles.append({
                "en": seg["text"],
                "ko": ko,
                "start": seg["start"],
                "end": seg["end"]
            })

        # 4. 분석 (광고 제외하고 본문만 - Claude가 알아서 처리)
        print("🤖 분석 중...")
        analysis = analyze(ep, transcript, client)

        # 5. 저장 (오디오는 WSJ 원본 URL 그대로 사용)
        out = {
            **ep,  # audio_url은 WSJ 원본 그대로
            "date": date_str,
            "subtitles": subtitles,
            "analysis": analysis,
            "ad_blocks_removed": 0,
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
        print(f"   자막: {len(subtitles)}개 (광고 포함)")

    finally:
        Path(raw_audio).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
