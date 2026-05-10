// =====================================================================
// WSJ The Journal · Korean Sync Player + Stock Chart + Claude Opinion
// =====================================================================

const EPISODES_INDEX = './data/episodes/index.json';   // [{date,title,...}, ...]
const LATEST_DIR     = './data/episodes/latest';

let sync = null;
let chart = null;
let chartSeries = null;
let player = null;

// ---------- 1. 메인 부트 ----------
async function boot() {
  const idx = await fetch(EPISODES_INDEX).then(r => r.json());
  renderEpisodeList(idx);
  await loadEpisode(idx[0].date);
  initChart();
  bindUI();
}

// ---------- 2. 에피소드 로드 ----------
async function loadEpisode(date) {
  const dir = `./data/episodes/${date}`;
  sync = await fetch(`${dir}/sync.json`).then(r => r.json());
  const audio = document.getElementById('audio');
  audio.src = `${dir}/audio.mp3`;

  document.getElementById('ep-title').textContent =
    `${sync.episode.date} · ${sync.episode.title}`;

  renderTranscript();
  renderSummary();

  player = new SyncPlayer(audio, sync);
}

// ---------- 3. 싱크 플레이어 ----------
class SyncPlayer {
  constructor(audio, sync) {
    this.audio = audio;
    this.sync = sync;
    this.cur = -1;
    this.skipAds = document.getElementById('skip-ads').checked;
    audio.addEventListener('timeupdate', () => this.tick());
  }
  tick() {
    const t = this.audio.currentTime;
    if (this.skipAds) {
      const ad = this.sync.ads.find(a => t >= a.start && t < a.end);
      if (ad) {
        this.audio.currentTime = ad.end + 0.05;
        this.notice(`⏭ 광고 ${Math.round(ad.duration)}초 스킵`);
        return;
      }
    }
    const i = this.findCue(t);
    if (i !== this.cur) {
      this.cur = i;
      this.highlight(i);
    }
  }
  findCue(t) {
    const c = this.sync.cues;
    let lo = 0, hi = c.length - 1, ans = -1;
    while (lo <= hi) {
      const m = (lo + hi) >> 1;
      if (c[m].start <= t) { ans = m; lo = m + 1; }
      else hi = m - 1;
    }
    return (ans >= 0 && t <= c[ans].end + 0.5) ? ans : -1;
  }
  highlight(i) {
    document.querySelectorAll('.cue').forEach(el => el.classList.remove('active'));
    if (i < 0) return;
    const el = document.getElementById(`cue-${i}`);
    if (el) {
      el.classList.add('active');
      el.scrollIntoView({behavior: 'smooth', block: 'center'});
    }
  }
  jump(i) {
    this.audio.currentTime = this.sync.cues[i].start;
    this.audio.play();
  }
  notice(msg) {
    const n = document.getElementById('ad-notice');
    n.textContent = msg;
    n.classList.add('show');
    clearTimeout(this._t);
    this._t = setTimeout(() => n.classList.remove('show'), 1800);
  }
}

// ---------- 4. 자막 렌더 (종목 태그 포함) ----------
function renderTranscript() {
  const box = document.getElementById('transcript-list');
  box.innerHTML = sync.cues.map((c, i) => {
    const tickers = (c.tickers || []).map(t =>
      `<button class="ticker" data-symbol="${t}" data-cue="${i}">$${t}</button>`
    ).join('');
    return `
      <div class="cue" id="cue-${i}">
        <div class="cue-time" data-jump="${i}">${fmt(c.start)}</div>
        <div class="cue-body">
          <p class="en">${escapeHtml(c.en)}</p>
          <p class="ko">${escapeHtml(c.ko)}</p>
          ${tickers ? `<div class="tickers">${tickers}</div>` : ''}
        </div>
      </div>`;
  }).join('');

  box.querySelectorAll('[data-jump]').forEach(el =>
    el.onclick = () => player.jump(+el.dataset.jump));

  box.querySelectorAll('.ticker').forEach(el =>
    el.onclick = () => onTickerClick(el.dataset.symbol, +el.dataset.cue));
}

// ---------- 5. 요약 / 에피소드 리스트 ----------
function renderSummary() {
  const s = sync.episode;
  document.getElementById('summary-content').innerHTML = `
    <h2>${escapeHtml(s.title)}</h2>
    <p class="meta">${s.date} · ${Math.round(sync.audio_duration / 60)}분</p>
    <h3>한글 요약</h3>
    <p>${escapeHtml(s.summary_ko || '')}</p>
    <h3>English Summary</h3>
    <p>${escapeHtml(s.summary_en || '')}</p>
    ${s.key_points_ko ? `
      <h3>핵심 포인트</h3>
      <ul>${s.key_points_ko.map(p => `<li>${escapeHtml(p)}</li>`).join('')}</ul>` : ''}
  `;
}

function renderEpisodeList(list) {
  document.getElementById('episode-list').innerHTML = list.slice(0, 10).map(e => `
    <div class="ep" data-date="${e.date}">
      <span class="ep-date">${e.date}</span>
      <span class="ep-title">${escapeHtml(e.title)}</span>
    </div>
  `).join('');
  document.querySelectorAll('.ep').forEach(el =>
    el.onclick = () => loadEpisode(el.dataset.date));
}

// ---------- 6. 종목 클릭 → 1년 차트 + Claude 의견 ----------
async function onTickerClick(symbol, cueIdx) {
  document.getElementById('stock-symbol').textContent = `$${symbol}`;
  document.getElementById('stock-name').textContent = '데이터 로딩 중…';
  document.getElementById('ai-opinion').textContent = 'Claude가 분석 중입니다…';
  document.getElementById('ai-rating').textContent = '...';
  document.getElementById('ai-rating').className = 'rating neutral';

  // 1) 1년 차트 데이터 (Yahoo Finance public endpoint)
  const end = Math.floor(Date.now() / 1000);
  const start = end - 365 * 24 * 3600;
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${symbol}` +
              `?period1=${start}&period2=${end}&interval=1d`;
  let bars, name = symbol, last = 0, prev = 0;
  try {
    const j = await fetch(url).then(r => r.json());
    const r = j.chart.result[0];
    name = r.meta.shortName || symbol;
    last = r.meta.regularMarketPrice;
    prev = r.meta.chartPreviousClose;
    bars = r.timestamp.map((t, i) => ({
      time: t,
      open: r.indicators.quote[0].open[i],
      high: r.indicators.quote[0].high[i],
      low:  r.indicators.quote[0].low[i],
      close:r.indicators.quote[0].close[i],
    })).filter(b => b.close != null);
  } catch (e) {
    document.getElementById('stock-name').textContent = '차트를 불러오지 못했습니다.';
    return;
  }

  document.getElementById('stock-name').textContent = name;
  const chg = ((last - prev) / prev) * 100;
  document.getElementById('stock-last').textContent = `$${last.toFixed(2)}`;
  const chgEl = document.getElementById('stock-chg');
  chgEl.textContent = `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%`;
  chgEl.className = 'chg ' + (chg >= 0 ? 'up' : 'down');

  // lightweight-charts 캔들
  chartSeries.setData(bars.map(b => ({
    time: b.time, open: b.open, high: b.high, low: b.low, close: b.close
  })));
  chart.timeScale().fitContent();

  // 2) Claude 매수/매도/중립 의견 (서버리스 함수 호출)
  fetchAIOpinion(symbol, sync.cues[cueIdx]?.en || '', sync.cues[cueIdx]?.ko || '');
}

async function fetchAIOpinion(symbol, ctxEn, ctxKo) {
  try {
    const res = await fetch('/api/analyze', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ symbol, context_en: ctxEn, context_ko: ctxKo })
    }).then(r => r.json());

    const map = { BUY: '매수', SELL: '매도', HOLD: '중립' };
    const cls = { BUY: 'buy', SELL: 'sell', HOLD: 'neutral' };
    const r = (res.rating || 'HOLD').toUpperCase();
    const rEl = document.getElementById('ai-rating');
    rEl.textContent = map[r] || '중립';
    rEl.className = 'rating ' + (cls[r] || 'neutral');
    document.getElementById('ai-opinion').textContent = res.opinion_ko || res.opinion || '';
  } catch (e) {
    document.getElementById('ai-opinion').textContent =
      'AI 분석을 불러오지 못했습니다. (API 키 또는 네트워크 확인)';
  }
}

// ---------- 7. 차트 초기화 ----------
function initChart() {
  const el = document.getElementById('chart');
  chart = LightweightCharts.createChart(el, {
    height: 320,
    layout: { background: {color: '#0e1116'}, textColor: '#cfd3dc' },
    grid: { vertLines: {color: '#1c2128'}, horzLines: {color: '#1c2128'} },
    timeScale: { timeVisible: false, borderColor: '#30363d' },
    rightPriceScale: { borderColor: '#30363d' },
  });
  chartSeries = chart.addCandlestickSeries({
    upColor: '#26a69a', downColor: '#ef5350',
    borderVisible: false, wickUpColor: '#26a69a', wickDownColor: '#ef5350'
  });
  new ResizeObserver(() => chart.applyOptions({width: el.clientWidth})).observe(el);
}

// ---------- 8. UI 바인딩 ----------
function bindUI() {
  document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(x => x.classList.add('hidden'));
    t.classList.add('active');
    document.getElementById('tab-' + t.dataset.tab).classList.remove('hidden');
  });
  document.getElementById('skip-ads').onchange = e => player.skipAds = e.target.checked;
  document.getElementById('show-en').onchange = e =>
    document.body.classList.toggle('hide-en', !e.target.checked);
  document.getElementById('speed').onchange = e =>
    document.getElementById('audio').playbackRate = +e.target.value;
}

// ---------- utils ----------
function fmt(t) {
  const m = Math.floor(t / 60), s = Math.floor(t % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}
function escapeHtml(s = '') {
  return s.replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

boot();