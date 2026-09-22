const API_BASE = ['127.0.0.1', 'localhost'].includes(window.location.hostname)
  ? 'http://127.0.0.1:18768'
  : '';

function apiUrl(path) {
  return `${API_BASE}${path}`;
}

const state = {
  bilibiliUrl: '',
  status: 'idle',
  song: null,
  currentLineIndex: 0,
  playerNonce: 0,
  playerSeekSeconds: 0,
  playerShouldAutoplay: false,
  playerUsesAbsoluteTime: false,
  isPlayingFlash: false,
  toast: '',
  recognitionNote: '',
  playerError: '',
  playbackMode: null,
  loopLineIndex: null,
  boundaryPauseInProgress: false,
  sync: {
    running: false,
    baseSeconds: 0,
    baseIsAbsoluteVideoTime: false,
    startedAt: 0,
    paused: false,
    pausedSeconds: 0,
    timer: null
  }
};

let pendingSync = null;

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function isBilibiliUrl(value) {
  return /bilibili\.com|b23\.tv/i.test(value || '');
}

function inferTitleFromUrl(url) {
  const bv = String(url || '').match(/BV[0-9A-Za-z]+/i)?.[0];
  return bv ? `B站歌曲 ${bv}` : '待识别歌曲';
}

function timeToSeconds(value) {
  if (typeof value === 'number') return Math.max(0, value);
  const text = String(value || '00:00').trim();
  const parts = text.split(':').map(Number);
  if (parts.length === 2) return (parts[0] || 0) * 60 + (parts[1] || 0);
  if (parts.length === 3) return (parts[0] || 0) * 3600 + (parts[1] || 0) * 60 + (parts[2] || 0);
  return 0;
}

function secondsToTime(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

function secondsToDisplayTime(value) {
  return secondsToTime(value);
}

function secondsToLrcTime(value) {
  const total = Math.max(0, Number(value) || 0);
  const minute = Math.floor(total / 60);
  const second = Math.floor(total % 60);
  const centisecond = Math.floor((total - Math.floor(total)) * 100);
  return `${String(minute).padStart(2, '0')}:${String(second).padStart(2, '0')}.${String(centisecond).padStart(2, '0')}`;
}

function normalizeLyrics(lyrics) {
  if (!Array.isArray(lyrics) || !lyrics.length) return [];
  return lyrics
    .filter(line => line && String(line.text || '').trim())
    .map((line, index) => {
      const seconds = Number.isFinite(Number(line.seconds)) ? Number(line.seconds) : timeToSeconds(line.time);
      const end = line.end === null || line.end === undefined ? NaN : Number(line.end);
      return {
        id: `line-${index}`,
        time: secondsToLrcTime(seconds),
        seconds,
        ...(Number.isFinite(end) ? { end } : {}),
        text: String(line.text).trim()
      };
    })
    .sort((a, b) => a.seconds - b.seconds);
}

function getRecognitionNote(source) {
  if (source === 'bilibili_subtitle') return '已接入 B站公开字幕，可以按字幕逐句练习。';
  if (source === 'lrclib') return '已通过歌词库自动匹配歌词，可以开始逐句练习。';
  if (source === 'known_lyrics') return '已自动匹配到歌词，可以开始逐句练习。';
  if (source === 'known_demo_lyrics') return '已自动匹配到歌词，可以开始逐句练习。';
  if (source === 'no_public_subtitle') return '已经查过 B站字幕和在线歌词库，暂时没找到可用歌词。';
  if (source === 'video_recognition_attempted') return '已经继续尝试视频字幕和画面识别，暂时没找到可用歌词。';
  if (source === 'video_recognition') return '已从视频字幕或画面识别出逐句文本。';
  if (source === 'demo') return '本地识别服务暂时不可用，当前显示演示数据。';
  return '已识别歌曲，正在使用可用歌词逐句练习。';
}

function fallbackSong(url) {
  return {
    title: inferTitleFromUrl(url),
    source: url,
    bvid: String(url || '').match(/BV[0-9A-Za-z]+/i)?.[0] || '',
    cid: '',
    videoStreamUrl: apiUrl(`/api/video?url=${encodeURIComponent(url)}`),
    videoWarning: '歌词识别服务暂时不可用，正在尝试直接播放本地视频。',
    videoOffsetSeconds: 0,
    lyricsSource: 'demo',
    lyrics: normalizeLyrics([
      { time: '00:00', text: '识别服务没有启动，先确认启动窗口是否还开着。' },
      { time: '00:06', text: '启动后再粘贴 B站链接，就会自动匹配歌词。' }
    ])
  };
}

function stopSync() {
  state.sync.running = false;
  pendingSync = null;
  state.sync.paused = false;
  state.sync.pausedSeconds = 0;
  window.clearInterval(state.sync.timer);
  state.sync.timer = null;
}

function lyricTimeToVideoTime(seconds) {
  return Math.max(0, Number(seconds || 0) + Number(state.song?.videoOffsetSeconds || 0));
}

function videoTimeToLyricTime(seconds) {
  return Math.max(0, Number(seconds || 0) - Number(state.song?.videoOffsetSeconds || 0));
}

function findLineIndexBySeconds(seconds) {
  const lyrics = state.song?.lyrics || [];
  if (!lyrics.length) return 0;
  let index = 0;
  for (let i = 0; i < lyrics.length; i += 1) {
    if (Number(lyrics[i].seconds) <= seconds + 0.15) index = i;
    else break;
  }
  return index;
}

function tickSync() {
  if (!state.sync.running || state.sync.paused || !state.song) return;
  const nativePlayer = document.querySelector('.native-player');
  if (state.playbackMode) return;
  if (nativePlayer && !nativePlayer.paused) {
    const nextIndex = findLineIndexBySeconds(videoTimeToLyricTime(nativePlayer.currentTime || 0));
    if (nextIndex !== state.currentLineIndex) {
      state.currentLineIndex = nextIndex;
      updateActiveLyricDom({ block: 'center' });
    }
    return;
  }
  const elapsed = (Date.now() - state.sync.startedAt) / 1000;
  const playbackSeconds = state.sync.baseSeconds + elapsed;
  const lyricSeconds = state.sync.baseIsAbsoluteVideoTime ? videoTimeToLyricTime(playbackSeconds) : playbackSeconds;
  const nextIndex = findLineIndexBySeconds(lyricSeconds);
  if (nextIndex !== state.currentLineIndex) {
    state.currentLineIndex = nextIndex;
    updateActiveLyricDom({ block: 'center' });
  }
}

function queueSync(seconds, options = {}) {
  pendingSync = { seconds: Math.max(0, Number(seconds) || 0), options };
  window.setTimeout(() => {
    if (pendingSync) startSync(pendingSync.seconds, pendingSync.options);
  }, 1200);
}

function startSync(seconds, options = {}) {
  state.sync.running = true;
  pendingSync = null;
  state.sync.baseSeconds = Math.max(0, Number(seconds) || 0);
  state.sync.baseIsAbsoluteVideoTime = Boolean(options.absoluteVideoTime);
  state.sync.startedAt = Date.now();
  state.sync.paused = false;
  state.sync.pausedSeconds = 0;
  window.clearInterval(state.sync.timer);
  state.sync.timer = window.setInterval(tickSync, 350);
  updateSyncStatusDom();
}

function getCurrentSyncSeconds() {
  if (!state.sync.running) return state.playerSeekSeconds || 0;
  if (state.sync.paused) return state.sync.pausedSeconds;
  return state.sync.baseSeconds + ((Date.now() - state.sync.startedAt) / 1000);
}

function pauseSync() {
  if (!state.sync.running || state.sync.paused) return;
  state.sync.pausedSeconds = getCurrentSyncSeconds();
  state.sync.paused = true;
  pendingSync = null;
  updateSyncStatusDom();
}

function resumeSync() {
  if (!state.sync.running || !state.sync.paused) return;
  state.sync.baseSeconds = state.sync.pausedSeconds;
  state.sync.startedAt = Date.now();
  state.sync.paused = false;
  updateSyncStatusDom();
}

function toggleSyncPause() {
  if (!state.sync.running) return;
  if (state.sync.paused) resumeSync();
  else pauseSync();
}

function showToast(message) {
  state.toast = message;
  let toast = document.querySelector('.toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.className = 'toast';
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    state.toast = '';
    toast?.remove();
  }, 1800);
}

function triggerPlayFlash() {
  state.isPlayingFlash = false;
}

async function identifySong() {
  const url = state.bilibiliUrl.trim();
  if (!url) {
    showToast('请先粘贴 B站链接。');
    return;
  }
  if (!isBilibiliUrl(url)) {
    showToast('目前只需要粘贴 B站链接。');
    return;
  }

  stopSync();
  state.status = 'identifying';
  state.song = null;
  state.currentLineIndex = 0;
  clearPlaybackMode();
  state.playerError = '';
  state.recognitionNote = '';
  render();

  try {
    const response = await fetch(apiUrl(`/api/identify?url=${encodeURIComponent(url)}`));
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || 'identify_failed');

    state.status = 'ready';
    state.song = {
      title: payload.title || inferTitleFromUrl(url),
      source: payload.source || url,
      bvid: payload.bvid || '',
      cid: payload.cid || '',
      videoStreamUrl: payload.videoStreamUrl ? apiUrl(payload.videoStreamUrl) : '',
      videoWarning: payload.videoWarning || '',
      videoOffsetSeconds: Number(payload.videoOffsetSeconds) || 0,
      lyricsSource: payload.lyricsSource || 'unknown',
      recognitionAttempts: payload.recognitionAttempts || [],
      lyrics: normalizeLyrics(payload.lyrics)
    };
    if (payload.lyricsSource === 'no_public_subtitle') {
      state.song.lyricsSource = payload.recognitionAttempts?.length ? 'video_recognition_attempted' : 'no_public_subtitle';
      state.song.lyrics = normalizeLyrics([
        { time: '00:00', text: '暂时没找到可用歌词。' }
      ]);
    } else if (!state.song.lyrics.length) {
      state.song.lyricsSource = payload.recognitionAttempts?.length ? 'video_recognition_attempted' : 'no_public_subtitle';
      state.song.lyrics = normalizeLyrics([
        { time: '00:00', text: '暂时没找到可用歌词。' },
        { time: '00:06', text: '可以换一个带字幕或歌名更明确的视频试试。' }
      ]);
    }
    state.playerSeekSeconds = 1;
    state.playerShouldAutoplay = true;
    state.playerUsesAbsoluteTime = true;
    state.currentLineIndex = findLineIndexBySeconds(videoTimeToLyricTime(1));
    queueSync(1, { absoluteVideoTime: true });
    state.playerNonce += 1;
    state.recognitionNote = getRecognitionNote(state.song.lyricsSource);
  } catch (error) {
    state.status = 'ready';
    state.song = fallbackSong(url);
    state.playerSeekSeconds = 1;
    state.playerShouldAutoplay = true;
    state.playerUsesAbsoluteTime = true;
    state.currentLineIndex = findLineIndexBySeconds(videoTimeToLyricTime(1));
    queueSync(1, { absoluteVideoTime: true });
    state.recognitionNote = getRecognitionNote('demo');
  }
  render();
}

function currentLine() {
  return state.song?.lyrics?.[state.currentLineIndex] || null;
}

function linePlaybackRange(index, duration = Infinity) {
  const lyrics = state.song?.lyrics || [];
  const line = lyrics[index];
  if (!line) return null;
  const start = lyricTimeToVideoTime(Number(line.seconds || 0));
  const next = lyrics[index + 1];
  const explicitEnd = line.end === null || line.end === undefined ? NaN : Number(line.end);
  const lyricEnd = Number.isFinite(explicitEnd)
    ? explicitEnd
    : Number(next?.seconds ?? (Number(line.seconds || 0) + 6));
  const rawEnd = lyricTimeToVideoTime(lyricEnd);
  const end = Number.isFinite(duration) && duration > 0 ? Math.min(rawEnd, duration) : rawEnd;
  return { index, start, end: Math.max(start, end) };
}

function clearPlaybackMode() {
  state.playbackMode = null;
  state.loopLineIndex = null;
  state.boundaryPauseInProgress = false;
}

function togglePlaybackMode(mode) {
  if (!state.song) return;
  if (state.playbackMode === mode) {
    clearPlaybackMode();
    updatePlaybackModeDom();
    return;
  }
  state.playbackMode = mode;
  state.boundaryPauseInProgress = false;
  if (mode === 'loop') {
    state.loopLineIndex = state.currentLineIndex;
    playCurrentLine();
  } else {
    state.loopLineIndex = null;
    updatePlaybackModeDom();
  }
}

function playCurrentLine(showMessage = false) {
  const line = currentLine();
  if (!state.song || !line) return;
  const seconds = Math.max(0, Number(line.seconds ?? timeToSeconds(line.time)));
  state.playerSeekSeconds = seconds;
  state.playerShouldAutoplay = true;
  state.playerUsesAbsoluteTime = false;
  state.playerNonce += 1;
  stopSync();
  queueSync(seconds);
  updatePlayerDom();
  updateActiveLyricDom({ block: 'nearest' });
  if (showMessage) showToast('已跳到当前句并尝试播放。');
}

function selectLine(index) {
  if (!state.song) return;
  state.currentLineIndex = Math.max(0, Math.min(index, state.song.lyrics.length - 1));
  if (state.playbackMode === 'loop') state.loopLineIndex = state.currentLineIndex;
  state.boundaryPauseInProgress = false;
  playCurrentLine();
}

function nextLine() {
  selectLine(state.currentLineIndex + 1);
}

function prevLine() {
  selectLine(state.currentLineIndex - 1);
}

function buildBilibiliPlayerUrl(seconds, options = {}) {
  if (!state.song?.bvid || !state.song?.cid) return '';
  const videoSeconds = options.absolute ? Math.max(0, Number(seconds) || 0) : lyricTimeToVideoTime(seconds);
  const params = new URLSearchParams({
    bvid: state.song.bvid,
    cid: String(state.song.cid),
    page: '1',
    high_quality: '1',
    autoplay: state.playerShouldAutoplay ? '1' : '0',
    danmaku: '0',
    t: String(Math.floor(videoSeconds)),
    _: String(state.playerNonce)
  });
  return `https://player.bilibili.com/player.html?${params.toString()}`;
}

function scrollActiveLyricIntoView() {
  const active = document.querySelector('.lyric-item.active');
  if (!active) return;
  active.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function syncLyricsPaneHeight() {
  const playerBox = document.querySelector('.player-box');
  const lyricsPane = document.querySelector('.lyrics-pane');
  if (!playerBox || !lyricsPane || window.matchMedia('(max-width: 900px)').matches) {
    if (lyricsPane) lyricsPane.style.height = '';
    return;
  }
  lyricsPane.style.height = `${Math.round(playerBox.getBoundingClientRect().height)}px`;
}

function updateActiveLyricDom(options = {}) {
  const items = document.querySelectorAll('.lyric-item');
  items.forEach((item, index) => {
    item.classList.toggle('active', index === state.currentLineIndex);
  });
  updateLineEditorDom();
  const active = document.querySelector('.lyric-item.active');
  if (active) active.scrollIntoView({ behavior: 'smooth', block: options.block || 'nearest' });
}

function updateLineEditorDom() {
  const editor = document.querySelector('.line-editor');
  const line = currentLine();
  if (!editor || !line) return;
  const timeNode = editor.querySelector('[data-current-line-time]');
  const textNode = editor.querySelector('[data-current-line-text]');
  if (timeNode) timeNode.textContent = secondsToDisplayTime(Number(line.seconds || 0));
  if (textNode) textNode.textContent = line.text || '';
  updatePlaybackModeDom();
}

function syncStatusText() {
  if (state.playbackMode === 'pause') return '逐句暂停';
  if (state.playbackMode === 'loop') return '单句循环';
  if (pendingSync) return '准备同步';
  if (!state.sync.running) return '未同步';
  return state.sync.paused ? '已暂停' : '跟随视频';
}

function updatePlaybackModeDom() {
  document.querySelectorAll('[data-playback-mode]').forEach(button => {
    const active = button.dataset.playbackMode === state.playbackMode;
    button.setAttribute('aria-pressed', String(active));
  });
  updateSyncStatusDom();
}

function updateSyncStatusDom() {
  const status = document.querySelector('[data-sync-status]');
  if (status) status.textContent = syncStatusText();
}

function updatePlayerDom() {
  const nativePlayer = document.querySelector('.native-player');
  if (nativePlayer) {
    const nextTime = lyricTimeToVideoTime(state.playerSeekSeconds);
    if (Number.isFinite(nativePlayer.duration) && nativePlayer.duration > 0 && nextTime > nativePlayer.duration - 0.2) {
      showToast('这句超出了当前视频长度，请换完整视频链接。');
      nativePlayer.currentTime = Math.max(0, nativePlayer.duration - 0.2);
    } else {
      nativePlayer.currentTime = nextTime;
    }
    nativePlayer.play().catch(() => {});
    startSync(state.playerSeekSeconds);
    return;
  }
  render();
}

function renderInputPanel() {
  const isLoading = state.status === 'identifying';
  return `
    <section class="input-panel" aria-labelledby="input-title">
      <div class="input-heading">
        <div>
          <h2 id="input-title">从一条视频链接开始</h2>
          <p>粘贴 B站歌曲视频地址，自动识别歌曲与可用歌词。</p>
        </div>
        <span class="input-step">Identify · Practice</span>
      </div>
      <div class="url-row">
        <label class="url-label" for="bilibili-url">B站歌曲链接</label>
        <input id="bilibili-url" class="url-input" value="${escapeHtml(state.bilibiliUrl)}" data-action="url-input" placeholder="https://www.bilibili.com/video/BV..." autocomplete="url" inputmode="url" ${isLoading ? 'disabled' : ''}>
        <button class="primary-btn" data-action="identify" ${isLoading ? 'disabled' : ''}>${isLoading ? '正在识别' : '识别歌曲'}</button>
      </div>
    </section>`;
}

function renderLyricsPane() {
  if (!state.song) return ''; 
  const line = currentLine();
  const adjustedLineTime = line ? secondsToDisplayTime(Number(line.seconds || 0)) : '00:00';
  return `
    <aside class="lyrics-pane" aria-label="逐句歌词">
      <div class="lyrics-toolbar">
        <div>
          <span class="panel-label">逐句练习</span>
          <strong data-sync-status>${escapeHtml(syncStatusText())}</strong>
        </div>
        <div class="lyrics-actions">
          <span class="lyrics-count">${state.song.lyrics.length} 句</span>
        </div>
      </div>

      <div class="line-editor">
        <div>
          <span>当前句</span>
          <strong data-current-line-time>${escapeHtml(adjustedLineTime)}</strong>
        </div>
        <p data-current-line-text>${escapeHtml(line?.text || '')}</p>
        <div class="playback-mode-actions" aria-label="播放模式">
          <button class="mode-btn" data-action="playback-mode" data-playback-mode="pause" aria-pressed="${state.playbackMode === 'pause'}">逐句暂停</button>
          <button class="mode-btn" data-action="playback-mode" data-playback-mode="loop" aria-pressed="${state.playbackMode === 'loop'}">单句循环</button>
        </div>
      </div>

      <div class="lyrics-list" data-lyrics-list>
        ${state.song.lyrics.map((item, index) => `
          <button class="lyric-item ${index === state.currentLineIndex ? 'active' : ''}" data-action="line" data-index="${index}">
            <span>${escapeHtml(secondsToDisplayTime(Number(item.seconds || 0)))}</span>
            <b>${escapeHtml(item.text)}</b>
          </button>
        `).join('')}
      </div>
    </aside>`;
}

function renderPractice() {
  if (state.status === 'identifying') {
    return `
      <section class="loading-card">
        <div class="loader"></div>
        <h2>正在准备练唱</h2>
        <p>正在识别歌曲、歌词和本地播放器。</p>
      </section>`;
  }

  if (!state.song) {
    return `
      <section class="empty-card">
        <div class="empty-icon" aria-hidden="true"><span></span><span></span><span></span><span></span></div>
        <h2>准备好你的练唱空间</h2>
        <p>输入歌曲视频链接后，播放器、歌词与逐句模式会在这里就位。</p>
        <p class="empty-hint">支持点击歌词定位，播放进度自动同步当前句。</p>
      </section>`;
  }

  const hasNativeVideo = Boolean(state.song.videoStreamUrl);
  const flashClass = state.isPlayingFlash ? 'playing-flash' : '';

  return `
    <section class="practice-card">
      <div class="song-head">
        <div>
          <span class="song-kicker">Now Practicing</span>
          <h2>${escapeHtml(state.song.title)}</h2>
          ${state.recognitionNote ? `<p class="recognition-note">${escapeHtml(state.recognitionNote)}</p>` : ''}
        </div>
      </div>

      <div class="practice-workspace">
        <section class="video-pane" aria-label="视频播放器">
          <div class="player-box ${flashClass}">
            ${hasNativeVideo ? `
              <video class="native-player" src="${escapeHtml(state.song.videoStreamUrl)}" controls autoplay playsinline></video>
              <div class="sync-pill">歌词跟随视频</div>
              ${state.playerError ? `<div class="player-error">${escapeHtml(state.playerError)}</div>` : ''}
            ` : `
              <div class="video-fallback">
                <strong>本地播放器准备失败</strong>
                <span>${escapeHtml(state.song.videoWarning || '当前视频文件太短或下载失败，请换一个完整的 B站歌曲链接。')}</span>
              </div>
            `}
          </div>
        </section>

        ${renderLyricsPane()}
      </div>
    </section>`;
}

function render() {
  document.getElementById('app').innerHTML = `
    <main class="page">
      <header class="brand-header">
        <div class="brand-lockup">
          <div class="brand-mark" aria-hidden="true"><span></span><span></span><span></span><span></span></div>
          <div>
            <h1 class="brand-name">Sing Reactor</h1>
            <p class="brand-tagline">歌曲识别、播放与逐句练唱工作台</p>
          </div>
        </div>
        <div class="brand-actions">
          <div class="brand-meta">让视频与歌词保持同一节拍<br>专注每一句的听唱练习</div>
          <a class="extension-download" href="./downloads/sing-reactor-extension-0.1.1.zip" download="sing-reactor-extension-0.1.1.zip" aria-label="下载 Sing Reactor 浏览器插件 0.1.1">
            <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
              <path d="m7 10 5 5 5-5"></path>
              <path d="M12 15V3"></path>
            </svg>
            <span>下载浏览器插件</span>
            <small>v0.1.1</small>
          </a>
        </div>
      </header>
      ${renderInputPanel()}
      ${renderPractice()}
    </main>
    ${state.toast ? `<div class="toast">${escapeHtml(state.toast)}</div>` : ''}
  `;
  window.requestAnimationFrame(scrollActiveLyricIntoView);
  window.requestAnimationFrame(syncLyricsPaneHeight);
}

function handleClick(event) {
  const target = event.target.closest('[data-action]');
  if (!target) return;
  const action = target.dataset.action;

  if (action === 'identify') identifySong();
  if (action === 'line') selectLine(Number(target.dataset.index));
  if (action === 'next') nextLine();
  if (action === 'prev') prevLine();
  if (action === 'practice-current') playCurrentLine(true);
  if (action === 'playback-mode') togglePlaybackMode(target.dataset.playbackMode);
}

function handleInput(event) {
  const urlTarget = event.target.closest('[data-action="url-input"]');
  if (urlTarget) {
    state.bilibiliUrl = urlTarget.value;
    return;
  }
}

function handleKeydown(event) {
  if (event.key === 'Enter' && event.target.matches('[data-action="url-input"]')) {
    identifySong();
  }
}

function handlePlayerMessage(event) {
  if (!String(event.origin || '').includes('bilibili.com')) return;
  const raw = typeof event.data === 'string' ? event.data : JSON.stringify(event.data || {});
  const text = raw.toLowerCase();
  if (text.includes('pause') || text.includes('paused')) pauseSync();
  if (text.includes('playing') || text.includes('play') || text.includes('resume')) resumeSync();
}

function handleVideoPlay(event) {
  if (!event.target.classList.contains('native-player')) return;
  if (state.boundaryPauseInProgress) {
    state.currentLineIndex = findLineIndexBySeconds(videoTimeToLyricTime(event.target.currentTime || 0));
    updateActiveLyricDom({ block: 'center' });
  }
  state.boundaryPauseInProgress = false;
  startSync(videoTimeToLyricTime(event.target.currentTime || 0));
}

function handleVideoPause(event) {
  if (!event.target.classList.contains('native-player')) return;
  pauseSync();
}

async function handleVideoError(event) {
  if (!event.target.classList.contains('native-player')) return;
  let message = '视频没有加载成功。';
  try {
    const response = await fetch(event.target.currentSrc || event.target.src);
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      message = payload?.message || `视频接口返回 ${response.status}`;
    }
  } catch (error) {
    message = error?.message || message;
  }
  state.playerError = message;
  const box = document.querySelector('.player-box');
  if (box && !box.querySelector('.player-error')) {
    const node = document.createElement('div');
    node.className = 'player-error';
    node.textContent = message;
    box.appendChild(node);
  } else if (box) {
    box.querySelector('.player-error').textContent = message;
  }
}

function handleVideoTimeUpdate(event) {
  const player = event.target;
  if (!player.classList.contains('native-player')) return;
  if (player.paused) return;

  if (state.playbackMode === 'loop' && state.loopLineIndex !== null) {
    const range = linePlaybackRange(state.loopLineIndex, player.duration);
    if (!range) return;
    if (state.currentLineIndex !== range.index) {
      state.currentLineIndex = range.index;
      updateActiveLyricDom({ block: 'nearest' });
    }
    if (player.currentTime < range.start - 0.15 || player.currentTime >= range.end - 0.12) {
      player.currentTime = range.start;
    }
    return;
  }

  if (state.playbackMode === 'pause' && !state.boundaryPauseInProgress) {
    const nextIndex = state.currentLineIndex + 1;
    const nextLine = state.song?.lyrics?.[nextIndex];
    if (nextLine) {
      const boundary = lyricTimeToVideoTime(Number(nextLine.seconds || 0));
      if (player.currentTime >= boundary - 0.12) {
        state.boundaryPauseInProgress = true;
        player.pause();
        player.currentTime = boundary;
        stopSync();
        updateSyncStatusDom();
        return;
      }
    }
  }

  const nextIndex = findLineIndexBySeconds(videoTimeToLyricTime(player.currentTime || 0));
  if (nextIndex !== state.currentLineIndex) {
    state.currentLineIndex = nextIndex;
    updateActiveLyricDom({ block: 'center' });
  }
}

function handleVideoEnded(event) {
  if (!event.target.classList.contains('native-player')) return;
  stopSync();
  state.boundaryPauseInProgress = false;
  state.loopLineIndex = state.playbackMode === 'loop' ? state.currentLineIndex : null;
  updateSyncStatusDom();
}

document.addEventListener('click', handleClick);
document.addEventListener('input', handleInput);
document.addEventListener('play', handleVideoPlay, true);
document.addEventListener('pause', handleVideoPause, true);
document.addEventListener('error', handleVideoError, true);
document.addEventListener('timeupdate', handleVideoTimeUpdate, true);
document.addEventListener('ended', handleVideoEnded, true);
document.addEventListener('keydown', handleKeydown);
window.addEventListener('message', handlePlayerMessage);
window.addEventListener('resize', syncLyricsPaneHeight);

render();
