// Global State
const PLATFORM_CONFIG = {
    instagram: {
        label: 'Instagram', placeholder: 'instagram.com/kullaniciadi veya @kullaniciadi',
        ranges: [[7, 'Son 1 Hafta'], [30, 'Son 1 Ay'], [90, 'Son 3 Ay'], [180, 'Son 6 Ay'], [365, 'Son 1 Yıl'], [1095, 'Son 3 Yıl']],
        defaultDays: 90, followerLabel: 'Takipçi', contentLabel: 'Gönderi', contentPlural: 'Gönderiler',
        videoShareLabel: 'Reels / Video Payı', description: 'Instagram hesabının seçilen dönemdeki gönderi ve Reels performansını inceleyin.'
    },
    tiktok: {
        label: 'TikTok', placeholder: 'tiktok.com/@kullanici veya @kullanici',
        ranges: [[7, 'Son 1 Hafta'], [30, 'Son 1 Ay'], [90, 'Son 3 Ay'], [180, 'Son 6 Ay'], [365, 'Son 1 Yıl'], [1095, 'Son 3 Yıl']],
        defaultDays: 90, followerLabel: 'Takipçi', contentLabel: 'İçerik', contentPlural: 'İçerikler',
        videoShareLabel: 'Video Payı', description: 'Herkese açık TikTok profilinin video ve fotoğraf/slayt içeriklerini inceleyin.'
    },
    youtube: {
        label: 'YouTube', placeholder: 'youtube.com/@kanal, kanal URL’si veya @kanal',
        ranges: [[180, 'Son 6 Ay'], [365, 'Son 1 Yıl'], [1095, 'Son 3 Yıl'], [2555, 'Son 7 Yıl']],
        defaultDays: 365, followerLabel: 'Abone', contentLabel: 'Video', contentPlural: 'Videolar ve Shorts',
        videoShareLabel: 'Shorts Payı', description: 'YouTube kanalının seçilen dönemdeki videolarını ve Shorts performansını inceleyin.'
    }
};
let activePlatform = 'instagram';
let currentData = { platform: 'instagram', profile: null, posts: [] };
let compareList = []; // Karşılaştırma hafızası
let APP_CONFIG = null;
let instagramLoggedIn = false;
let currentFetchRequestId = 0;
let tableRenderState = { posts: [], visibleCount: 0, batchSize: 12, observer: null, containerId: 'tableBody' };

// Initialize Lucide Icons
window.lucide?.createIcons?.();

// Utils
function escapeHTML(value) {
    return String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
}
function platformOf(entity = {}, fallback = activePlatform) {
    return Object.hasOwn(PLATFORM_CONFIG, entity.platform) ? entity.platform : fallback;
}
function safePlatformUrl(value, platform = activePlatform) {
    const hosts = {
        instagram: ['instagram.com', 'www.instagram.com'],
        tiktok: ['tiktok.com', 'www.tiktok.com', 'm.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'],
        youtube: ['youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be']
    };
    try {
        const url = new URL(String(value));
        if (url.protocol === 'https:' && !url.username && !url.password && hosts[platform]?.includes(url.hostname.toLowerCase())) return escapeHTML(url.href);
    } catch (error) { }
    return '#';
}
function safeInstagramUrl(value) { return safePlatformUrl(value, 'instagram'); }
function accountKey(platform, username) {
    return platform + ':' + String(username || '').trim().replace(/^@/, '').toLowerCase();
}
function compareDisplayName(account) {
    return PLATFORM_CONFIG[account.platform].label + ' · ' + accountDisplayName(account.profile, account.platform);
}
function dataSourceNote(payload) {
    const meta = payload.meta || {};
    const profile = payload.profile || {};
    const platform = platformOf(payload);
    const parts = [];
    if (payload.demo || meta.source === 'demo') parts.push('DEMO: Tamamen yapay örnek veriler; gerçek bir hesabın sonucu değildir.');
    const sources = {
        instagram_web: 'Instagram oturumuyla gözlenen veriler',
        youtube_public_innertube: 'YouTube herkese açık kanal sayfaları',
        youtube_public_watch: 'YouTube herkese açık video sayfaları',
        youtube_data_api_v3: 'YouTube Data API',
        youtube_public_listing: 'YouTube herkese açık içerik listesi',
        tiktok_ytdlp: 'TikTok herkese açık profil içeriği',
        tiktok_embed: 'TikTok herkese açık gömülü profil örneklemi',
        tiktok_public_embed: 'TikTok herkese açık gömülü profil örneklemi'
    };
    if (meta.source && meta.source !== 'demo') parts.push('Kaynak: ' + (sources[meta.source] || PLATFORM_CONFIG[platform].label + ' verileri') + '.');
    if (meta.metrics_source) {
        const labels = String(meta.metrics_source).split('+').map(source => sources[source]).filter(Boolean);
        if (labels.length) parts.push('Metrikler: ' + labels.join(' + ') + '.');
    }
    parts.push('İncelenen içerik: ' + (Array.isArray(payload.posts) ? payload.posts.length : 0) + '.');
    if (meta.source_limited || profile.content_count_is_sample) parts.push('Kısmi örneklem; seçilen dönemin tamamını kapsamayabilir.');
    if (meta.truncated) parts.push('İçerik sınırına ulaşıldı; tüm paylaşımlar kapsanmayabilir.');
    if (meta.metrics_limited) parts.push('Bazı metrikler sunulmadı; boş değerler sıfır değildir.');
    if (meta.metric_coverage) {
        const names = {likes: 'beğeni', comments: 'yorum', views: 'izlenme'};
        const coverage = Object.entries(meta.metric_coverage).filter(([key, value]) => names[key] && Number.isInteger(value?.available) && Number.isInteger(value?.total)).map(([key, value]) => `${names[key]} ${value.available}/${value.total}`);
        if (coverage.length) parts.push('Metrik kapsamı: ' + coverage.join(', ') + '.');
    }
    if (meta.note) parts.push(String(meta.note));
    return parts.join(' ');
}
function fmt(n) {
    if (n === -1) return 'Gizli';
    if (n == null || n === '') return '—';
    n = Number(n);
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return n.toLocaleString('tr-TR');
}

function formatCurrency(n) {
    if (n == null || Number.isNaN(Number(n))) return '—';
    return Number(n).toLocaleString('tr-TR', { maximumFractionDigits: 0 }) + ' ₺';
}

function formatPercent(n, digits = 2) {
    if (n == null || Number.isNaN(Number(n))) return '0';
    const value = Number(n);
    const maximumFractionDigits = value !== 0 && Math.abs(value) < 0.01 ? Math.max(digits, 4) : digits;
    return value.toLocaleString('tr-TR', { minimumFractionDigits: 0, maximumFractionDigits });
}

function toast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 3000);
}

function showEl(id, show) {
    const el = document.getElementById(id);
    if (el) el.style.display = show ? 'block' : 'none';
}

function showError(boxId, textId, msg) {
    document.getElementById(textId).textContent = msg;
    showEl(boxId, true);
}

function getPostTypeLabel(post = {}, sourcePlatform = activePlatform) {
    if (post.content_type) return String(post.content_type);
    const platform = platformOf(post, sourcePlatform);
    const category = String(post.category || '').toLowerCase();
    const typename = String(post.typename || '').toLowerCase();
    if (platform === 'youtube') return category.includes('short') || typename.includes('short') ? 'Shorts' : 'Video';
    if (platform === 'tiktok') return ['photo', 'image', 'slide', 'carousel'].some(token => category.includes(token) || typename.includes(token)) ? 'Fotoğraf / Slayt' : 'Video';
    if (category.includes('reel') || typename.includes('reel')) return 'Reels';
    if (post.is_video) return 'Video';
    if (post.typename === 'GraphSidecar' || post.category === 'Carousel' || ((post.media_count || 1) > 1 && !post.is_video)) return 'Carousel';
    return 'Fotoğraf';
}

function normalizeMetric(value) {
    if (value == null || value === '' || typeof value === 'boolean') return null;
    const normalized = Number(value);
    return Number.isFinite(normalized) && normalized >= 0 ? normalized : null;
}

function getPostViews(post = {}, sourcePlatform = activePlatform) {
    for (const key of ['views', 'video_play_count', 'play_count', 'ig_play_count']) {
        const value = normalizeMetric(post[key]);
        if (value != null) return value;
    }

    // Instagram's legacy video_view_count can be zero or stale for Reels.
    // If exact play_count is unavailable, show no value instead of a wrong one.
    if (platformOf(post, sourcePlatform) === 'instagram' && getPostTypeLabel(post, sourcePlatform) === 'Reels') return null;

    for (const key of ['video_view_count', 'view_count']) {
        const value = normalizeMetric(post[key]);
        if (value != null) return value;
    }
    return null;
}

function summarizePostsForOverview(posts = []) {
    const visibleLikes = posts.filter(p => typeof p.likes === 'number' && p.likes !== -1);
    const visibleComments = posts.filter(p => typeof p.comments === 'number' && p.comments !== -1);
    const videoPosts = posts.filter(p => ['Video', 'Reels', 'Shorts'].includes(getPostTypeLabel(p)));
    const visibleViews = posts
        .map(p => getPostViews(p))
        .filter(value => typeof value === 'number');
    const stats = currentData.stats || {};
    const avgLikes = Object.hasOwn(stats, 'avg_likes') ? normalizeMetric(stats.avg_likes)
        : (visibleLikes.length ? Math.round(visibleLikes.reduce((sum, p) => sum + p.likes, 0) / visibleLikes.length) : null);
    const avgComments = Object.hasOwn(stats, 'avg_comments') ? normalizeMetric(stats.avg_comments)
        : (visibleComments.length ? Math.round(visibleComments.reduce((sum, p) => sum + p.comments, 0) / visibleComments.length) : null);
    const avgViews = Object.hasOwn(stats, 'avg_views') ? normalizeMetric(stats.avg_views)
        : (visibleViews.length ? Math.round(visibleViews.reduce((sum, value) => sum + value, 0) / visibleViews.length) : null);
    const videoCount = videoPosts.length;
    const carouselCount = posts.filter(p => getPostTypeLabel(p) === 'Carousel').length;
    const imageCount = posts.filter(p => getPostTypeLabel(p) === 'Fotoğraf').length;
    return {
        avgLikes,
        avgComments,
        avgViews,
        engagementRate: currentData.stats?.engagement_rate == null ? null : Number(currentData.stats.engagement_rate),
        reelsShare: posts.length ? (videoCount / posts.length) * 100 : null,
        carouselShare: posts.length ? (carouselCount / posts.length) * 100 : null,
        imageShare: posts.length ? (imageCount / posts.length) * 100 : null,
        analyzedPosts: posts.length,
    };
}

function cleanupTableObserver() {
    if (tableRenderState.observer) {
        tableRenderState.observer.disconnect();
        tableRenderState.observer = null;
    }
}

function updateTableLoadStatus() {
    const statusEl = document.getElementById('tableLoadStatus');
    const sentinel = document.getElementById('tableSentinel');
    if (!statusEl || !sentinel) return;

    const contentLabel = (PLATFORM_CONFIG[activePlatform]?.contentLabel || 'İçerik').toLocaleLowerCase('tr-TR');
    if (!tableRenderState.posts.length) {
        statusEl.textContent = `Gösterilecek ${contentLabel} bulunamadı.`;
        sentinel.style.display = 'none';
        return;
    }

    const loaded = Math.min(tableRenderState.visibleCount, tableRenderState.posts.length);
    const hasMore = loaded < tableRenderState.posts.length;
    statusEl.textContent = hasMore
        ? `Kaydırdıkça yükleniyor • ${loaded}/${tableRenderState.posts.length} ${contentLabel} görünür`
        : `Tüm içerikler yüklendi • ${tableRenderState.posts.length} ${contentLabel}`;
    sentinel.style.display = hasMore ? 'block' : 'none';
}

function appendNextPostRows() {
    const tbody = document.getElementById(tableRenderState.containerId);
    if (!tbody) return;

    const nextPosts = tableRenderState.posts.slice(tableRenderState.visibleCount, tableRenderState.visibleCount + tableRenderState.batchSize);
    if (!nextPosts.length) {
        updateTableLoadStatus();
        cleanupTableObserver();
        return;
    }

    const html = nextPosts.map(p => {
        const tagType = getPostTypeLabel(p);
        const views = getPostViews(p) ?? null;
        const contentUrl = safePlatformUrl(p.url, platformOf(p, currentData.platform));
        return `
        <tr>
            <td>${escapeHTML(p.date || '—')}</td>
            <td><a href="${contentUrl}" target="_blank" rel="noopener noreferrer" style="color:var(--accent-color); font-weight:500;">İçeriği Gör <i data-lucide="external-link" style="width:14px; height:14px; vertical-align:middle;"></i></a></td>
            <td><span class="tag tag-video">${escapeHTML(tagType)}</span></td>
            <td>${fmt(p.likes)}</td>
            <td>${fmt(p.comments)}</td>
            <td>${fmt(views)}</td>
        </tr>
        `;
    }).join('');

    tbody.insertAdjacentHTML('beforeend', html);
    tableRenderState.visibleCount += nextPosts.length;
    updateTableLoadStatus();
    window.lucide?.createIcons?.();

    if (tableRenderState.visibleCount >= tableRenderState.posts.length) {
        cleanupTableObserver();
    }
}

function setupTableInfiniteScroll() {
    cleanupTableObserver();
    const sentinel = document.getElementById('tableSentinel');
    if (!sentinel) return;

    tableRenderState.observer = new IntersectionObserver(entries => {
        if (entries.some(entry => entry.isIntersecting)) {
            appendNextPostRows();
        }
    }, { rootMargin: '220px 0px 220px 0px' });

    tableRenderState.observer.observe(sentinel);
}

function setPlatform(platform) {
    if (!PLATFORM_CONFIG[platform]) return;
    activePlatform = platform;
    const config = PLATFORM_CONFIG[platform];
    document.getElementById('toast')?.classList.remove('show');
    document.body.dataset.platform = platform;
    document.querySelectorAll('.platform-btn').forEach(button => {
        const selected = button.dataset.platform === platform;
        button.classList.toggle('active', selected);
        button.setAttribute('aria-pressed', selected ? 'true' : 'false');
    });

    const input = document.getElementById('igInput');
    if (input) {
        input.placeholder = config.placeholder;
        input.value = '';
    }
    const range = document.getElementById('timeRange');
    if (range) {
        range.innerHTML = config.ranges.map(([value, label]) =>
            `<option value="${value}"${value === config.defaultDays ? ' selected' : ''}>${label}</option>`
        ).join('');
    }
    const heading = document.getElementById('dashboardHeading');
    if (heading) heading.textContent = `${config.label} Genel Bakış`;
    const description = document.getElementById('dashboardDescription');
    if (description) description.textContent = config.description;
    const loadingText = document.getElementById('fetchLoadingText');
    if (loadingText) loadingText.textContent = `Veriler ${config.label} kaynağından çekiliyor...`;
    const tableTitle = document.getElementById('contentTableTitle');
    if (tableTitle) tableTitle.textContent = `Son ${config.contentPlural}`;
    const authPanel = document.getElementById('instagramAuthPanel');
    if (authPanel) authPanel.style.display = platform === 'instagram' ? '' : 'none';
    const compareInput = document.getElementById('compareInput');
    if (compareInput) compareInput.placeholder = config.placeholder;
    closeLoginModal();
    refreshWelcome();


    document.querySelectorAll('[data-instagram-only="true"]').forEach(item => {
        const disabled = platform !== 'instagram';
        item.classList.toggle('platform-disabled', disabled);
        item.setAttribute('aria-disabled', disabled ? 'true' : 'false');
        item.title = disabled ? 'Bu özellik yalnız Instagram hesapları için kullanılabilir.' : '';
        item.tabIndex = disabled ? -1 : 0;
    });
    const activePage = document.querySelector('.nav-item.active');
    if (activePage?.classList.contains('platform-disabled')) {
        document.querySelector('.nav-item[data-tab="dashboard"]')?.click();
    }

    currentFetchRequestId += 1;
    currentData = { platform, profile: null, posts: [] };
    cleanupTableObserver();
    showEl('fetchResults', false);
    showEl('fetchLoading', false);
    showEl('fetchErrorBox', false);
    const fetchButton = document.getElementById('fetchBtn');
    if (fetchButton) fetchButton.disabled = false;
    const note = document.getElementById('platformDataNote');
    if (note) note.style.display = 'none';
    renderCompareUI();
    window.lucide?.createIcons?.();
}

function accountDisplayName(profile = {}, sourcePlatform = activePlatform) {
    const username = String(profile.username || '');
    if (!username) return profile.full_name || (sourcePlatform === 'youtube' ? 'Kanal' : 'Hesap');
    return username.startsWith('@') ? username : '@' + username;
}
function refreshWelcome() {
    const instagram = activePlatform === 'instagram';
    const config = PLATFORM_CONFIG[activePlatform];
    document.getElementById('welcomeKicker').textContent = 'Kendi bilgisayarınızda • ' + config.label + ' analizi';
    document.getElementById('welcomeHeading').textContent = instagram ? 'Instagram oturumunuzu bağlayın, hesabı analiz edin.' : config.label + ' hesabını doğrudan analiz edin.';
    document.getElementById('welcomeDescription').textContent = instagram
        ? 'Session ID veya kullanıcı adı ve şifreyle bağlanın. Kendi kontrolünüzdeki ayrı/ikincil bir hesabı da kullanabilirsiniz. Session ID’nin ne olduğu ve nasıl alınacağı giriş ekranında adım adım açıklanır. Oturum bilgilerinizi şifreniz gibi gizli tutun.'
        : activePlatform === 'youtube'
            ? 'Herkese açık YouTube kanalının videoları ve Shorts içerikleri için oturum veya API anahtarı gerekmez. Kanal bağlantısını girin. Gizli metrikler, yuvarlanmış abone sayıları ve eksik tarih bilgileri kapsamı sınırlayabilir; bunlar gerçek değer yerine tahminle doldurulmaz.'
            : 'Herkese açık TikTok profili için oturum veya API anahtarı gerekmez. Kullanıcı adını ya da profil bağlantısını girin. TikTok erişimi ve sunduğu örneklem sınırlı olabilir; video/fotoğraf sayısı hesabın tüm içerik sayısı anlamına gelmeyebilir.';
    showEl('welcomeConnectButton', instagram);
    showEl('welcomeCard', !instagram || !instagramLoggedIn);
}
// Navigation
document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', (e) => {
        e.preventDefault();
        if (item.getAttribute('aria-disabled') === 'true') return;
        document.querySelectorAll('.nav-item').forEach(x => x.classList.remove('active'));
        document.querySelectorAll('.page').forEach(x => x.classList.remove('active'));
        item.classList.add('active');
        const targetPage = 'page-' + item.dataset.tab;
        const pageEl = document.getElementById(targetPage);
        if (pageEl) pageEl.classList.add('active');
        window.lucide?.createIcons?.();
    });
});

// Credentials are sent to the local server only and never stored in the browser.
let currentLoginTab = 'cookie';
function switchLoginTab(tab) {
    currentLoginTab = tab === 'password' ? 'password' : 'cookie';
    for (const method of ['cookie', 'password']) {
        const active = method === currentLoginTab;
        const suffix = method === 'cookie' ? 'Cookie' : 'Password';
        document.getElementById('tab' + suffix).classList.toggle('active', active);
        document.getElementById('tab' + suffix).setAttribute('aria-selected', String(active));
        document.getElementById('login' + suffix + 'Pane').classList.toggle('active', active);
    }
    document.getElementById('cookieInput').value = '';
    document.getElementById('igPassword').value = '';
    document.getElementById('loginError').style.display = 'none';
    document.getElementById('loginSubmitBtn').textContent = currentLoginTab === 'cookie' ? 'Session ID ile Bağlan' : 'Instagram’a Giriş Yap';
}
function openLoginModal() {
    if (activePlatform !== 'instagram') return;
    document.getElementById('loginModal').classList.add('active');
    document.getElementById('loginError').style.display = 'none';
    document.getElementById(currentLoginTab === 'cookie' ? 'cookieInput' : 'igUsername').focus();
}
function closeLoginModal() {
    document.getElementById('loginModal').classList.remove('active');
    document.getElementById('cookieInput').value = '';
    document.getElementById('igPassword').value = '';
}
document.getElementById('loginModal').addEventListener('click', e => { if (e.target.id === 'loginModal') closeLoginModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') { closeLoginModal(); closeInfoModal(); } });
document.getElementById('cookieInput').addEventListener('keydown', e => { if (e.key === 'Enter') submitLogin(); });
document.getElementById('igPassword').addEventListener('keydown', e => { if (e.key === 'Enter') submitLogin(); });

async function submitLogin() {
    const errEl = document.getElementById('loginError');
    const submitBtn = document.getElementById('loginSubmitBtn');
    const input = document.getElementById('cookieInput');
    const passwordInput = document.getElementById('igPassword');
    const isCookie = currentLoginTab === 'cookie';
    const payload = isCookie
        ? { sessionid: input.value.trim() }
        : { username: document.getElementById('igUsername').value.trim(), password: passwordInput.value };
    errEl.style.display = 'none';
    if (isCookie ? !payload.sessionid : (!payload.username || !payload.password)) {
        errEl.textContent = isCookie ? 'Lütfen sessionid çerezinin Value değerini girin.' : 'Kullanıcı adı ve şifre gerekli.';
        errEl.style.display = 'block';
        return;
    }
    submitBtn.disabled = true;
    submitBtn.textContent = 'Doğrulanıyor...';
    input.value = '';
    passwordInput.value = '';
    try {
        const res = await fetch(isCookie ? '/api/login_cookie' : '/api/login_password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const d = await res.json();
        if (!res.ok) {
            errEl.textContent = d.error || 'Bağlantı kurulamadı.';
            errEl.style.display = 'block';
            return;
        }
        closeLoginModal();
        setLogin(true, d.username);
        toast('Instagram oturumu bağlandı: @' + d.username);
    } catch (e) {
        errEl.textContent = 'Bağlantı hatası. Uygulamanın çalıştığını kontrol edin.';
        errEl.style.display = 'block';
    } finally {
        input.value = '';
        passwordInput.value = '';
        submitBtn.disabled = false;
        submitBtn.textContent = currentLoginTab === 'cookie' ? 'Session ID ile Bağlan' : 'Instagram’a Giriş Yap';
    }
}

async function doLogout() {
    try {
        const response = await fetch('/api/logout', { method: 'POST' });
        if (!response.ok) { toast('Yerel oturum silinemedi. Tekrar deneyin.'); return; }
        currentFetchRequestId += 1;
        cleanupTableObserver();
        setLogin(false);
        currentData = { platform: activePlatform, profile: null, posts: [] };
        compareList = compareList.filter(account => account.platform !== 'instagram');
        renderCompareUI();
        showEl('fetchResults', false);
        toast('Yerel oturum silindi. Instagram oturumunu ayrıca Instagram’dan kapatabilirsiniz.');
    } catch (e) { toast('Çıkış sırasında bağlantı kurulamadı.'); }
}

function setLogin(isLoggedIn, username) {
    instagramLoggedIn = Boolean(isLoggedIn);
    const dot = document.getElementById('loginDot');
    dot.className = 'status-indicator' + (isLoggedIn ? ' active' : '');
    document.getElementById('loginInfoText').textContent = isLoggedIn ? '@' + (username || 'user') : 'Giriş yapılmadı';
    showEl('loginBtn', !isLoggedIn);
    showEl('logoutBtn', isLoggedIn);
    refreshWelcome();
}

function applyAiAvailability() {
    const enabled = Boolean(APP_CONFIG?.ai_enabled);
    document.querySelectorAll('[data-ai-feature]').forEach(button => {
        button.disabled = !enabled;
        button.title = enabled ? 'Kendi Gemini anahtarınızla çalışır.' : 'İsteğe bağlı: .env dosyasına GEMINI_API_KEY ekleyin.';
    });
    document.getElementById('aiStatusNote').textContent = enabled
        ? 'Gemini özellikleri açık. AI raporunu veya İçerik DNA analizini başlatmanız halinde seçilen içerik Gemini’ye gönderilir; sağlayıcı ücreti uygulanabilir.'
        : 'Temel analiz, karşılaştırma ve Excel için AI anahtarı gerekmez. AI PDF için isteğe bağlı Gemini API anahtarı ekleyebilirsiniz. İçerik DNA ve sponsorluk yalnız Instagram için kullanılabilir.';
}

/* =======================================
   1. GENEL BAKIŞ (FETCH)
   ======================================= */
document.getElementById('igInput').addEventListener('keydown', e => { if (e.key === 'Enter') fetchData(); });

async function fetchData(demo = false) {
    const input = demo ? 'demo_account' : document.getElementById('igInput').value.trim();
    const requestPlatform = activePlatform;
    const requestId = ++currentFetchRequestId;
    const config = PLATFORM_CONFIG[requestPlatform];
    if (!input) {
        showError('fetchErrorBox', 'fetchErrorText', `Lütfen bir ${config.label} kullanıcı adı veya profil bağlantısı girin.`);
        return;
    }

    showEl('fetchErrorBox', false);
    showEl('fetchResults', false);
    showEl('fetchLoading', true);
    document.getElementById('fetchBtn').disabled = true;
    const days = parseInt(document.getElementById('timeRange').value, 10) || config.defaultDays;

    try {
        const res = await fetch('/api/fetch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ input, platform: requestPlatform, days, demo })
        });
        const d = await res.json();
        if (requestId !== currentFetchRequestId || requestPlatform !== activePlatform) return;

        if (!res.ok) {
            if (d.suggest_login && requestPlatform === 'instagram') {
                setLogin(false, '');
                openLoginModal();
            }
            showError('fetchErrorBox', 'fetchErrorText', d.error || 'Hata oluştu');
            return;
        }
        currentData = { ...d, demo, platform: requestPlatform, profile: {...d.profile, platform: requestPlatform}, posts: (d.posts || []).map(post => ({...post, platform: requestPlatform})), period_days: days };
        renderProfileOverview('profileCard', currentData.profile);
        renderKPIs('summaryGrid', currentData.posts);
        renderTable('tableBody', currentData.posts);

        const note = document.getElementById('platformDataNote');
        if (note) {
            const noteText = dataSourceNote(currentData);
            note.textContent = noteText;
            note.style.display = noteText ? 'block' : 'none';
        }
        showEl('fetchResults', true);
        toast(demo ? 'Yapay demo verileri gösteriliyor.' : config.label + ' verileri başarıyla çekildi.');
    } catch (e) {
        if (requestId === currentFetchRequestId) {
            showError('fetchErrorBox', 'fetchErrorText', 'Bağlantı hatası: ' + e.message);
        }
    } finally {
        if (requestId === currentFetchRequestId) {
            showEl('fetchLoading', false);
            document.getElementById('fetchBtn').disabled = false;
            window.lucide?.createIcons?.();
        }
    }
}
function loadDemo() {
    document.querySelector('.nav-item[data-tab="dashboard"]')?.click();
    closeLoginModal();
    return fetchData(true);
}

async function generateReportPDF() {
    if (!APP_CONFIG?.ai_enabled) { toast('AI PDF için kendi Gemini API anahtarınızı ekleyin.'); return; }
    if (!currentData || !currentData.profile) {
        toast('Lütfen önce analiz yapın.');
        return;
    }
    const reportData = currentData;
    const btn = document.getElementById('pdfBtn');
    const ogText = btn.innerHTML;
    btn.innerHTML = '<i data-lucide="loader-2" class="spin"></i> Rapor Hazırlanıyor...';
    btn.disabled = true;

    try {
        const res = await fetch('/api/generate_report', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(reportData)
        });
        
        if (!res.ok) {
            const d = await res.json();
            toast(d.error || 'Rapor oluşturulamadı');
            return;
        }

        const blob = await res.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = (reportData.platform || 'socialmedia') + '_' + String(reportData.profile.username || 'hesap').replace(/[^a-zA-Z0-9_.-]+/g, '_') + "_rapor.pdf";
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);
        toast('PDF İndirildi');
    } catch (e) {
        toast('Bağlantı hatası: ' + e.message);
    } finally {
        btn.innerHTML = ogText;
        btn.disabled = false;
        window.lucide?.createIcons?.();
    }
}

function renderProfileOverview(containerId, p) {
    const c = document.getElementById(containerId);
    const config = PLATFORM_CONFIG[platformOf(p)];
    const initial = escapeHTML((p.username || p.full_name || '?')[0].toUpperCase());
    const picUrl = p.profile_pic_url ? proxyPic(p.profile_pic_url) : '';
    const avatar = picUrl ? `<img src="${picUrl}" onerror="this.style.display='none'">` : initial;
    const isContentSample = Boolean(p.content_count_is_sample);
    const contentCount = isContentSample ? p.sampled_content_count : p.post_count;
    const contentLabel = isContentSample ? 'İncelenen Örneklem' : config.contentLabel;

    c.innerHTML = `
        <div class="profile-avatar">${avatar}</div>
        <div class="profile-info">
            <h2>${escapeHTML(p.full_name || p.username)}</h2>
            <p>${escapeHTML(accountDisplayName(p))}</p>
        </div>
        <div class="profile-stats">
            <div class="stat-box">
                <div class="stat-val">${fmt(p.followers)}</div>
                <div class="stat-lbl">${config.followerLabel}</div>
            </div>
            <div class="stat-box">
                <div class="stat-val">${fmt(contentCount)}</div>
                <div class="stat-lbl">${contentLabel}</div>
            </div>
        </div>
    `;
}

function renderKPIs(containerId, posts) {
    const summary = summarizePostsForOverview(posts);
    const config = PLATFORM_CONFIG[activePlatform];
    const shareValue = activePlatform === 'youtube' ? (posts.length ? posts.filter(post => getPostTypeLabel(post) === 'Shorts').length / posts.length * 100 : null) : summary.reelsShare;
    const kpis = [
        { icon: 'bar-chart-2', value: summary.engagementRate == null ? 'Mevcut değil' : `%${formatPercent(summary.engagementRate, 2)}`, label: 'Etkileşim Oranı', tone: 'k-green' },
        { icon: 'heart', value: summary.avgLikes == null ? 'Mevcut değil' : fmt(summary.avgLikes), label: 'Ort. Beğeni', tone: 'k-green' },
        { icon: 'message-circle', value: summary.avgComments == null ? 'Mevcut değil' : fmt(summary.avgComments), label: 'Ort. Yorum', tone: 'k-orange' },
        { icon: 'eye', value: summary.avgViews == null ? 'Mevcut değil' : fmt(summary.avgViews), label: 'Ort. İzlenme', tone: 'k-purple' },
        { icon: 'clapperboard', value: shareValue == null ? 'Mevcut değil' : `%${formatPercent(shareValue, 1)}`, label: config.videoShareLabel, tone: 'k-purple' },
        { icon: 'files', value: fmt(summary.analyzedPosts), label: `Analiz Edilen ${config.contentLabel}`, tone: 'k-orange' },
    ];

    document.getElementById(containerId).innerHTML = kpis.map(item => `
        <div class="kpi-card ${item.tone}">
            <div class="kpi-icon"><i data-lucide="${item.icon}"></i></div>
            <div class="kpi-val">${escapeHTML(item.value)}</div>
            <div class="kpi-lbl">${escapeHTML(item.label)}</div>
        </div>
    `).join('');
}
function renderTable(containerId, posts) {
    const tbody = document.getElementById(containerId);
    if (!tbody) return;

    tableRenderState = {
        ...tableRenderState,
        posts: Array.isArray(posts) ? posts : [],
        visibleCount: 0,
        containerId,
    };

    tbody.innerHTML = '';
    updateTableLoadStatus();

    if (!tableRenderState.posts.length) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-muted);">Gösterilecek gönderi bulunamadı.</td></tr>';
        cleanupTableObserver();
        return;
    }

    appendNextPostRows();
    setupTableInfiniteScroll();
}

async function exportExcel() {
    if (!currentData?.profile || !currentData.posts?.length) {
        toast('Lütfen önce bir analiz çalıştırın.');
        return;
    }
    const exportData = currentData;
    try {
        const res = await fetch('/api/export_excel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                platform: exportData.platform,
                profile: exportData.profile,
                posts: exportData.posts,
                stats: exportData.stats,
                meta: exportData.meta,
                period_days: exportData.period_days,
                demo: exportData.demo
            })
        });
        if (!res.ok) {
            const error = await res.json().catch(() => ({}));
            toast(error.error || 'Excel oluşturulamadı');
            return;
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        const account = (exportData.profile?.username || 'hesap').replace(/[^a-zA-Z0-9_.-]+/g, '_');
        a.download = `${exportData.platform}_${account}_analiz.xlsx`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        toast('Excel dosyası indirildi');
    } catch (e) {
        toast('Excel indirme hatası: ' + e.message);
    }
}
/* =======================================
   1.5 KARŞILAŞTIRMA (COMPARE)
   ======================================= */
let cmpCharts = {};

function proxyPic(url) {
    if (!url) return '';
    return '/api/proxy_pic?url=' + encodeURIComponent(url);
}

function buildCompareEntry(payload) {
    const platform = platformOf(payload);
    const posts = (payload.posts || []).map(post => ({...post, platform}));
    const stats = payload.stats || {};
    const average = (field, resolver = post => normalizeMetric(post[field])) => {
        if (Object.hasOwn(stats, 'avg_' + field)) return normalizeMetric(stats['avg_' + field]);
        const values = posts.map(resolver).filter(value => value != null);
        return values.length ? Math.round(values.reduce((total, value) => total + value, 0) / values.length) : null;
    };
    const avgLikes = average('likes');
    const avgComments = average('comments');
    const avgViews = average('views', post => getPostViews(post, platform));
    const followers = normalizeMetric(payload.profile?.followers);
    const engRate = Object.hasOwn(stats, 'engagement_rate') ? normalizeMetric(stats.engagement_rate)
        : (avgLikes != null && avgComments != null && followers > 0 ? Number(((avgLikes + avgComments) / followers * 100).toFixed(4)) : null);
    const cats = {};
    posts.forEach(post => { const category = getPostTypeLabel(post, platform); cats[category] = (cats[category] || 0) + 1; });
    const contentDist = Object.fromEntries(Object.entries(cats).map(([category, count]) => [category, posts.length ? Math.round(count / posts.length * 100) : 0]));
    const videos = posts.filter(post => ['Video', 'Reels', 'Shorts'].includes(getPostTypeLabel(post, platform)));
    const measured = posts.filter(post => normalizeMetric(post.likes) != null || normalizeMetric(post.comments) != null);
    const engagementOf = post => (normalizeMetric(post.likes) ?? 0) + (normalizeMetric(post.comments) ?? 0);
    const best = measured.length ? measured.reduce((best, post) => engagementOf(post) > engagementOf(best) ? post : best, measured[0]) : null;
    return {
        key: accountKey(platform, payload.profile?.username), platform,
        profile: {...payload.profile, platform}, avgLikes, avgComments, avgViews, engRate,
        videoRatio: posts.length ? Math.round(videos.length / posts.length * 100) : null,
        shortsRatio: posts.length ? Math.round(posts.filter(post => getPostTypeLabel(post, platform) === 'Shorts').length / posts.length * 100) : null,
        contentDist,
        bestPost: best ? {url: best.url, likes: best.likes, comments: best.comments, contentType: getPostTypeLabel(best, platform), date: best.date} : null,
        postsAnalyzed: posts.length,
        dataNote: dataSourceNote({...payload, platform}),
        demo: Boolean(payload.demo || payload.meta?.source === 'demo'),
        likesUnavailable: avgLikes == null, commentsUnavailable: avgComments == null,
        viewsUnavailable: avgViews == null, engagementUnavailable: engRate == null
    };
}

async function fetchCompareItem() {
    const inputElement = document.getElementById('compareInput');
    if (!inputElement) return;
    const input = inputElement.value.trim();
    const requestPlatform = activePlatform;
    const requestId = currentFetchRequestId;
    if (!input) { toast('Lütfen bir kullanıcı adı girin'); return; }
    try {
        const response = await fetch('/api/fetch', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({input, platform: requestPlatform, days: PLATFORM_CONFIG[requestPlatform].defaultDays, max_posts: 50, demo: false})
        });
        const data = await response.json();
        if (requestPlatform !== activePlatform || requestId !== currentFetchRequestId) return;
        if (!response.ok) { toast(data.error || 'Hesap eklenemedi'); return; }
        const entry = buildCompareEntry({...data, platform: requestPlatform});
        if (compareList.some(account => account.key === entry.key)) { toast('Bu hesap zaten listede'); return; }
        if (compareList.length >= 4) compareList.shift();
        compareList.push(entry);
        inputElement.value = '';
        renderCompareUI();
    } catch (error) { toast('Bağlantı hatası'); }
}

function addToCompare() {
    if (!currentData.profile) return;
    const entry = buildCompareEntry(currentData);
    if (compareList.some(account => account.key === entry.key)) { toast('Bu hesap zaten karşılaştırma listesinde!'); return; }
    if (compareList.length >= 4) compareList.shift();
    compareList.push(entry);
    renderCompareUI();
    toast(compareDisplayName(entry) + ' karşılaştırmaya eklendi (' + compareList.length + ' hesap)');
}
function removeFromCompare(key) {
    compareList = compareList.filter(account => account.key !== key);
    renderCompareUI();
    toast('Hesap listeden çıkarıldı');
}

document.addEventListener('DOMContentLoaded', async () => {
    setPlatform('instagram');
    try {
        const [configResponse, loginResponse] = await Promise.all([
            fetch('/api/config'), fetch('/api/login_status')
        ]);
        if (configResponse.ok) APP_CONFIG = await configResponse.json();
        if (loginResponse.ok) {
            const login = await loginResponse.json();
            setLogin(Boolean(login.logged_in), login.username || '');
        }
    } catch (e) { toast('Yerel uygulamayla bağlantı kurulamadı.'); }
    applyAiAvailability();
});

function clearCompare() {
    compareList = [];
    renderCompareUI();
    toast('Karşılaştırma listesi temizlendi');
}
const CMP_COLORS = ['#3b82f6', '#8b5cf6', '#10b981', '#f59e0b', '#ef4444'];

function renderCompareUI() {
    // Destroy old charts
    Object.values(cmpCharts).forEach(c => { try { c.destroy(); } catch(e){} });
    cmpCharts = {};

    if (compareList.length === 0) {
        showEl('compareEmptyState', true);
        showEl('compareContent', false);
        document.getElementById('compareSummaryStrip').innerHTML = '';
        return;
    }
    showEl('compareEmptyState', false);
    showEl('compareContent', true);

    const accounts = compareList;
    const labels = accounts.map(compareDisplayName);

    const maxF = Math.max(...accounts.map(a => Number(a.profile.followers) || 0), 1);
    const maxL = Math.max(...accounts.map(a => a.avgLikes ?? 0), 1);
    const maxV = Math.max(...accounts.map(a => a.avgViews ?? 0), 1);
    const maxC = Math.max(...accounts.map(a => a.avgComments ?? 0), 1);
    const maxE = Math.max(...accounts.map(a => a.engRate ?? 0), 0.01);
    const noEngagementData = accounts.every(a => a.engagementUnavailable);
    const noViewData = accounts.every(a => a.viewsUnavailable);

    const weights = APP_CONFIG?.etki_oranlari || {izlenme: .35, begeni: .25, yorum: .25, takipci: .05, etkilesim: .10};
    accounts.forEach(account => {
        const candidates = [[normalizeMetric(account.profile.followers), maxF, weights.takipci], [account.avgLikes, maxL, weights.begeni], [account.avgViews, maxV, weights.izlenme], [account.avgComments, maxC, weights.yorum], [account.engRate, maxE, weights.etkilesim]];
        const available = candidates.filter(([value, maximum, weight]) => value != null && typeof weight === 'number' && weight > 0);
        const sum = available.reduce((total, [value, maximum, weight]) => total + value / maximum * 100 * weight, 0);
        const activeWeight = available.reduce((total, [value, maximum, weight]) => total + weight, 0);
        account.total_score = activeWeight ? sum / activeWeight : null;
    });
    const scored = accounts.filter(account => account.total_score != null);
    const leader = scored.length ? scored.reduce((best, account) => account.total_score > best.total_score ? account : best, scored[0]) : null;
    const engagementLeader = accounts.reduce((best, account) => (account.engRate ?? -1) > (best.engRate ?? -1) ? account : best, accounts[0]);
    const reachLeader = accounts.reduce((best, account) => (account.avgViews ?? -1) > (best.avgViews ?? -1) ? account : best, accounts[0]);

    document.getElementById('compareSummaryStrip').innerHTML = [
        {
            icon: 'crown',
            label: 'Örneklem Performansı',
            value: leader ? compareDisplayName(leader) : 'Mevcut değil',
            detail: leader ? `${Math.round(leader.total_score)}/100 · mevcut metriklerle` : 'Ölçülen metrik yok',
        },
        {
            icon: 'activity',
            label: noEngagementData ? 'Etkileşim Verisi' : 'En Yüksek Etkileşim',
            value: noEngagementData ? 'Mevcut değil' : compareDisplayName(engagementLeader),
            detail: noEngagementData ? 'Bu örneklemde sunulmadı' : `%${formatPercent(engagementLeader.engRate, 2)} oran`,
        },
        {
            icon: 'eye',
            label: noViewData ? 'İzlenme Verisi' : 'En Güçlü İzlenme',
            value: noViewData ? 'Mevcut değil' : compareDisplayName(reachLeader),
            detail: noViewData ? 'Bu örneklemde sunulmadı' : `${fmt(reachLeader.avgViews)} ort. izlenme`,
        }
    ].map(item => `
        <div class="compare-summary-chip">
            <div class="compare-summary-icon"><i data-lucide="${item.icon}"></i></div>
            <div>
                <span>${escapeHTML(item.label)}</span>
                <strong>${escapeHTML(item.value)}</strong>
                <small>${escapeHTML(item.detail)}</small>
            </div>
        </div>
    `).join('');

    // ======= 1. Profil Kartları =======
    document.getElementById('compareProfileCards').innerHTML = accounts.map((x, i) => {
        const p = x.profile;
        const isLeader = leader?.key === x.key;
        const sourceConfig = PLATFORM_CONFIG[x.platform];
        const picUrl = proxyPic(p.profile_pic_url);
        const avatarHtml = picUrl ? `<img src="${picUrl}" style="width:56px;height:56px;border-radius:50%;object-fit:cover;" onerror="this.style.display='none'">`
            : `<div class="compare-avatar-fallback">${escapeHTML((p.username || '?')[0].toUpperCase())}</div>`;

        return `
        <div class="compare-profile-card ${isLeader ? 'is-leader' : ''}" style="border-top: 3px solid ${CMP_COLORS[i]}; --compare-accent:${CMP_COLORS[i]};">
            <button class="compare-remove-btn" data-remove-account="${escapeHTML(x.key)}" title="Kaldır"><i data-lucide="x" style="width:14px;height:14px;"></i></button>
            <div class="compare-card-topline">
                <span class="compare-rank-pill">${x.demo ? 'Yapay Demo' : (isLeader ? 'Lider' : 'Karşılaştırma')}</span>
                <span class="compare-score-pill">${x.total_score == null ? 'Skor mevcut değil' : 'Skor ' + Math.round(x.total_score) + '/100'}</span>
            </div>
            <div style="display:flex; align-items:center; gap:14px; margin-bottom:16px;">
                ${avatarHtml}
                <div>
                    <strong style="font-size:15px;">${escapeHTML(compareDisplayName(x))}</strong>
                    ${p.is_verified ? '<i data-lucide="badge-check" style="width:14px;height:14px;color:#3b82f6;vertical-align:middle;margin-left:4px;"></i>' : ''}
                    <div style="font-size:12px;color:var(--text-muted);">${escapeHTML(p.full_name || '')}</div>
                </div>
            </div>
            <div class="compare-card-metrics">
                <div><span class="compare-metric-val">${fmt(p.followers)}</span><span class="compare-metric-lbl">${sourceConfig.followerLabel}</span></div>
                <div><span class="compare-metric-val">${x.likesUnavailable ? 'Mevcut değil' : fmt(x.avgLikes)}</span><span class="compare-metric-lbl">Ort. Beğeni</span></div>
                <div><span class="compare-metric-val">${x.viewsUnavailable ? 'Mevcut değil' : fmt(x.avgViews)}</span><span class="compare-metric-lbl">Ort. İzlenme</span></div>
                <div><span class="compare-metric-val">${x.engagementUnavailable ? 'Mevcut değil' : `%${formatPercent(x.engRate, 2)}`}</span><span class="compare-metric-lbl">Etkileşim</span></div>
            </div>
            <div class="compare-mini-track"><span style="width:${x.total_score == null ? 0 : Math.max(0, Math.round(x.total_score))}%; background:${CMP_COLORS[i]};"></span></div>
            <div class="compare-card-footnotes">
                <span><i data-lucide="film"></i> ${sourceConfig.videoShareLabel} ${x.videoRatio == null ? '—' : '%' + fmt(x.platform === 'youtube' ? x.shortsRatio : x.videoRatio)}</span>
                <span><i data-lucide="files"></i> ${fmt(x.postsAnalyzed)} ${sourceConfig.contentLabel.toLowerCase()}</span>
            </div>
            <p class="compare-data-note">${escapeHTML(x.dataNote)}</p>
        </div>`;
    }).join('');

    // ======= 2. Radar Chart =======
    cmpCharts.radar = new ApexCharts(document.querySelector("#compareRadarChart"), {
        series: accounts.map((a, i) => ({
            name: compareDisplayName(a),
            data: [
                a.profile.followers == null ? null : Math.round(a.profile.followers / maxF * 100),
                a.avgLikes == null ? null : Math.round(a.avgLikes / maxL * 100),
                a.avgViews == null ? null : Math.round(a.avgViews / maxV * 100),
                a.avgComments == null ? null : Math.round(a.avgComments / maxC * 100),
                a.engRate == null ? null : Math.round(a.engRate / maxE * 100),
            ]
        })),
        chart: { type: 'radar', height: 340, toolbar: { show: false }, fontFamily: 'Inter, sans-serif' },
        xaxis: { categories: ['Takipçi / Abone', 'Beğeni', 'İzlenme', 'Yorum', 'Etkileşim Oranı'] },
        colors: CMP_COLORS.slice(0, accounts.length),
        stroke: { width: 2 },
        fill: { opacity: 0.15 },
        markers: { size: 3 },
        yaxis: { show: false },
        tooltip: {
            y: { formatter: (val) => val + '/100' }
        }
    });
    cmpCharts.radar.render();

    // ======= 3. Bar Chart: Takipçi + Ort. İzlenme + Ort. Beğeni =======
    cmpCharts.bar = new ApexCharts(document.querySelector("#compareBarChart"), {
        series: [
            { name: 'Takipçi / Abone', data: accounts.map(a => a.profile.followers) },
            { name: 'Ort. İzlenme', data: accounts.map(a => a.avgViews) },
            { name: 'Ort. Beğeni', data: accounts.map(a => a.avgLikes) },
        ],
        chart: { type: 'bar', height: 340, toolbar: { show: false }, fontFamily: 'Inter, sans-serif' },
        plotOptions: { bar: { columnWidth: '55%', borderRadius: 4 } },
        dataLabels: { enabled: false },
        xaxis: { categories: labels },
        yaxis: { labels: { formatter: (val) => fmt(val) } },
        colors: ['#3b82f6', '#8b5cf6', '#10b981'],
        tooltip: { y: { formatter: (val) => fmt(val) } }
    });
    cmpCharts.bar.render();

    // ======= 4. İçerik Dağılımı Stacked Bar =======
    const allCats = [...new Set(accounts.flatMap(a => Object.keys(a.contentDist)))];
    const catColors = { 'Reels': '#d946ef', 'Fotoğraf': '#3b82f6', 'Carousel': '#f59e0b', 'Video': '#10b981', 'Shorts': '#ef4444', 'Fotoğraf / Slayt': '#06b6d4', 'Diğer': '#94a3b8' };

    cmpCharts.content = new ApexCharts(document.querySelector("#compareContentChart"), {
        series: allCats.map(cat => ({
            name: cat,
            data: accounts.map(a => a.contentDist[cat] || 0)
        })),
        chart: { type: 'bar', height: 340, stacked: true, stackType: '100%', toolbar: { show: false }, fontFamily: 'Inter, sans-serif' },
        plotOptions: { bar: { horizontal: true, borderRadius: 4 } },
        xaxis: { categories: labels },
        colors: allCats.map(c => catColors[c] || '#94a3b8'),
        tooltip: { y: { formatter: (val) => '%' + val } },
        legend: { position: 'bottom' }
    });
    cmpCharts.content.render();

    // ======= 5. En İyi Gönderiler =======
    document.getElementById('compareBestPosts').innerHTML = accounts.map((x, i) => {
        const bp = x.bestPost;
        if (!bp) return `<div class="compare-best-item"><strong style="color:${CMP_COLORS[i]}">${escapeHTML(compareDisplayName(x))}</strong><span style="color:var(--text-muted);font-size:12px;">Veri yok</span></div>`;
        return `
        <div class="compare-best-item">
            <div style="display:flex;align-items:center;gap:10px;flex:1;">
                <div style="width:8px;height:40px;border-radius:4px;background:${CMP_COLORS[i]};"></div>
                <div>
                    <strong>${escapeHTML(compareDisplayName(x))}</strong>
                    <div style="font-size:12px;color:var(--text-muted);">${escapeHTML(bp.contentType || 'Diğer')} — ${escapeHTML(bp.date)}</div>
                </div>
            </div>
            <div style="display:flex;gap:16px;align-items:center;">
                <span style="font-size:13px;"><i data-lucide="heart" style="width:14px;height:14px;color:#ef4444;vertical-align:middle;"></i> ${fmt(bp.likes)}</span>
                <span style="font-size:13px;"><i data-lucide="message-circle" style="width:14px;height:14px;color:#3b82f6;vertical-align:middle;"></i> ${fmt(bp.comments)}</span>
                <a href="${safePlatformUrl(bp.url, x.platform)}" target="_blank" class="btn btn-sm btn-outline">Gör</a>
            </div>
        </div>`;
    }).join('');

    // ======= 6. Detaylı Metrik Tablosu (Satır bazlı) =======
    const headerCells = accounts.map((a, i) => `<th style="border-bottom:3px solid ${CMP_COLORS[i]};min-width:140px;">${escapeHTML(compareDisplayName(a))}</th>`).join('');
    document.querySelector('#compareTable thead tr').innerHTML = `<th>Metrik</th>${headerCells}`;

    function bestInRow(vals, higher) {
        const numericValues = vals.filter(v => typeof v === 'number' && Number.isFinite(v));
        if (!numericValues.length) return -1;
        const bestValue = higher ? Math.max(...numericValues) : Math.min(...numericValues);
        return vals.findIndex(v => v === bestValue);
    }

    const metrics = [
        { label: 'Takipçi / Abone Sayısı', key: a => a.profile.followers, higher: true },
        { label: 'Hesabın Toplam İçeriği (varsa)', key: a => a.profile.content_count_is_sample ? null : a.profile.post_count, higher: true },
        { label: 'Ort. Beğeni', key: a => a.avgLikes, higher: true },
        { label: 'Ort. Yorum ', key: a => a.avgComments, higher: true },
        { label: 'Ort. İzlenme (Video)', key: a => a.avgViews, higher: true },
        { label: 'Etkileşim Oranı (%)', key: a => a.engRate, higher: true },
        { label: 'Video / Reels / Shorts Oranı (%)', key: a => a.videoRatio, higher: null },
        { label: 'Analiz Edilen İçerik', key: a => a.postsAnalyzed, higher: null },
    ];

    const rows = metrics.map(m => {
        const vals = accounts.map(a => m.key(a));
        const best = m.higher !== null ? bestInRow(vals, m.higher) : -1;
        const cells = vals.map((v, i) => {
            const isBest = i === best;
            const style = isBest ? 'font-weight:700;color:var(--success);' : '';
            const icon = isBest ? ' <i data-lucide="trophy" style="width:12px;height:12px;vertical-align:middle;color:var(--warning);"></i>' : '';
            const formattedValue = v == null ? 'Mevcut değil' : (typeof v === 'number' && m.label.includes('%') ? `%${formatPercent(v, 2)}` : fmt(v));
            return `<td style="${style}">${formattedValue}${icon}</td>`;
        }).join('');
        return `<tr><td style="font-weight:600;color:var(--text-secondary);">${m.label}</td>${cells}</tr>`;
    }).join('');

    document.getElementById('compareTableBody').innerHTML = rows;

    // ======= 7. Teknik Analiz Yorumu =======
    const notes = generateTechnicalNotes(accounts);
    document.getElementById('compareTechnicalNotes').innerHTML = notes.map(n => `
        <div class="strategy-item" style="border-left-color:${n.color};">
            <div class="strategy-icon"><i data-lucide="${n.icon}"></i></div>
            <div class="strategy-content">
                <h4>${escapeHTML(n.title)}</h4>
                <p>${escapeHTML(n.detail)}</p>
            </div>
        </div>
    `).join('');

    document.querySelectorAll('[data-remove-account]').forEach(button => button.addEventListener('click', () => removeFromCompare(button.dataset.removeAccount)));
    window.lucide?.createIcons?.();
}

function generateTechnicalNotes(accounts) {
    if (accounts.length < 2) return [];
    const measured = accounts.filter(account => account.engRate != null);
    const best = [...measured].sort((a, b) => b.engRate - a.engRate)[0];
    const notes = [{
        icon: 'bar-chart-2', color: 'var(--text-secondary)',
        title: 'Seçilen hesapların özeti',
        detail: accounts.map(a => `${compareDisplayName(a)}: ${fmt(a.profile.followers)} ${PLATFORM_CONFIG[a.platform].followerLabel.toLowerCase()}, ${a.engRate == null ? 'etkileşim mevcut değil' : '%' + formatPercent(a.engRate, 2) + ' etkileşim'}`).join(' | ')
    }];
    if (best) notes.unshift({
        icon: 'activity', color: 'var(--success)',
        title: 'Örneklemde en yüksek etkileşim',
        detail: `${compareDisplayName(best)} hesabının ölçülen gönderilerinde etkileşim oranı %${formatPercent(best.engRate, 2)}. Farklı dönem ve örneklem büyüklükleri sonuçları etkileyebilir.`
    });
    if (new Set(accounts.map(account => account.platform)).size > 1) notes.push({icon: 'info', color: 'var(--warning)', title: 'Platformlar farklı metrikler sunar', detail: 'Takipçi ve abone, izlenme ve etkileşim farklı platformlarda farklı anlam taşır. Tarih aralığı, örneklem ve eksik metrik kapsamını birlikte değerlendirin. Skor yalnız mevcut metrikler üzerinden hesaplanır; erişim veya kalite garantisi değildir.'});
    if (accounts.some(account => account.demo)) notes.push({
        icon: 'info', color: 'var(--warning)', title: 'Demo verisi var',
        detail: 'Bu karşılaştırmada yapay demo verisi bulunuyor. Gerçek hesap performansı olarak yorumlamayın.'
    });
    return notes;
}

/* =======================================
   3. SPONSORLUK ROI
   ======================================= */
document.getElementById('spInput').addEventListener('keydown', e => { if (e.key === 'Enter') fetchSponsorship(); });

function setSponsorshipPdfButtonState(disabled) {
    const btn = document.getElementById('spPdfBtn');
    if (btn) btn.disabled = Boolean(disabled);
}

function buildSponsorshipPdfCapture(pageEl) {
    const wrapper = document.createElement('div');
    wrapper.style.position = 'fixed';
    wrapper.style.left = '-20000px';
    wrapper.style.top = '0';
    wrapper.style.zIndex = '-1';
    wrapper.style.width = `${Math.ceil(pageEl.getBoundingClientRect().width)}px`;
    wrapper.style.padding = '0';
    wrapper.style.background = '#f5f7fb';

    const clone = pageEl.cloneNode(true);
    clone.style.display = 'block';
    clone.style.width = '100%';
    clone.style.margin = '0';

    const searchCard = clone.querySelector('.search-card');
    if (searchCard) searchCard.remove();

    const loadingState = clone.querySelector('#spLoading');
    if (loadingState) loadingState.remove();

    const errorBox = clone.querySelector('#spErrorBox');
    if (errorBox && errorBox.style.display === 'none') {
        errorBox.remove();
    }

    const results = clone.querySelector('#spResults');
    if (results) results.style.display = 'block';

    wrapper.appendChild(clone);
    return wrapper;
}

function waitForImages(rootEl) {
    const images = Array.from(rootEl.querySelectorAll('img'));
    if (!images.length) return Promise.resolve();

    return Promise.all(images.map(img => {
        if (img.complete && img.naturalWidth > 0) {
            return Promise.resolve();
        }

        return new Promise(resolve => {
            const done = () => resolve();
            img.addEventListener('load', done, { once: true });
            img.addEventListener('error', done, { once: true });
        });
    }));
}

function getSponsorshipPdfFilename() {
    const raw = document.getElementById('spInput')?.value.trim() || 'sponsorluk-roi';
    return raw.replace(/[\\/:*?"<>|\s]+/g, '_') + '_sponsorluk_roi.pdf';
}

async function exportSponsorshipPDF() {
    const pageEl = document.getElementById('page-sponsorship');
    const resultsEl = document.getElementById('spResults');
    const btn = document.getElementById('spPdfBtn');
    const jsPDF = window.jspdf && window.jspdf.jsPDF;

    if (!pageEl || !resultsEl || resultsEl.style.display === 'none') {
        toast('Önce sponsorluk teklifi oluşturun.');
        return;
    }

    if (typeof window.html2canvas !== 'function' || typeof jsPDF !== 'function') {
        toast('PDF araçları yüklenemedi. Sayfayı yenileyip tekrar deneyin.');
        return;
    }

    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-2" class="spin"></i> PDF Hazırlanıyor';
    window.lucide?.createIcons?.();

    const captureRoot = buildSponsorshipPdfCapture(pageEl);
    document.body.appendChild(captureRoot);

    try {
        if (document.fonts && document.fonts.ready) {
            await document.fonts.ready;
        }
        await waitForImages(captureRoot);

        const canvas = await window.html2canvas(captureRoot, {
            scale: 2,
            useCORS: true,
            backgroundColor: '#f5f7fb',
            logging: false,
            width: captureRoot.scrollWidth,
            windowWidth: captureRoot.scrollWidth,
            scrollX: 0,
            scrollY: 0,
        });

        const pdf = new jsPDF('p', 'mm', 'a4');
        const margin = 8;
        const pageWidth = pdf.internal.pageSize.getWidth() - (margin * 2);
        const pageHeight = pdf.internal.pageSize.getHeight() - (margin * 2);
        const imgHeight = (canvas.height * pageWidth) / canvas.width;
        const imgData = canvas.toDataURL('image/jpeg', 0.96);

        let heightLeft = imgHeight;
        let yOffset = margin;

        pdf.addImage(imgData, 'JPEG', margin, yOffset, pageWidth, imgHeight);
        heightLeft -= pageHeight;

        while (heightLeft > 0) {
            pdf.addPage();
            yOffset = margin - (imgHeight - heightLeft);
            pdf.addImage(imgData, 'JPEG', margin, yOffset, pageWidth, imgHeight);
            heightLeft -= pageHeight;
        }

        pdf.save(getSponsorshipPdfFilename());
        toast('Sponsorluk PDF indirildi');
    } catch (e) {
        console.error('Sponsorluk PDF hatası:', e);
        toast('PDF oluşturulamadı: ' + e.message);
    } finally {
        captureRoot.remove();
        btn.innerHTML = originalText;
        btn.disabled = false;
        window.lucide?.createIcons?.();
    }
}

async function fetchSponsorship() {
    if (activePlatform !== 'instagram') { toast('Sponsorluk analizi yalnız Instagram için kullanılabilir.'); return; }
    const input = document.getElementById('spInput').value.trim();
    const pageType = document.getElementById('spPageType') ? document.getElementById('spPageType').value : 'influencer';
    const days = parseInt(document.getElementById('spTimeRange')?.value, 10) || 90;
    if (!input) { showError('spErrorBox', 'spErrorText', 'Lütfen hedef hesap girin.'); return; }

    showEl('spErrorBox', false);
    showEl('spResults', false);
    showEl('spLoading', true);
    document.getElementById('spFetchBtn').disabled = true;
    setSponsorshipPdfButtonState(true);

    try {
        const res = await fetch('/api/sponsorship', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ input, page_type: pageType, days, demo: false })
        });
        const d = await res.json();
        if (!res.ok) {
            if (d.suggest_login) { setLogin(false, ''); openLoginModal(); }
            showError('spErrorBox', 'spErrorText', d.error || 'Hata oluştu');
            return;
        }
        renderSponsorshipData(d);
        showEl('spResults', true);
        setSponsorshipPdfButtonState(false);
        toast('Sponsorluk teklifleri hazırlandı');
    } catch (e) {
        showError('spErrorBox', 'spErrorText', 'Bağlantı hatası: ' + e.message);
    } finally {
        showEl('spLoading', false);
        document.getElementById('spFetchBtn').disabled = false;
        setSponsorshipPdfButtonState(document.getElementById('spResults').style.display === 'none');
        window.lucide?.createIcons?.();
    }
}

function getConfidenceLabel(score) {
    if (score >= 85) return 'Yüksek Güven';
    if (score >= 70) return 'Orta-Yüksek Güven';
    if (score >= 55) return 'Orta Güven';
    return 'Temkinli Güven';
}

function renderSponsorshipProfile(data) {
    const p = data.profile || {};
    const c = document.getElementById('spProfileCard');
    const initial = escapeHTML((p.username || '?')[0].toUpperCase());
    const picUrl = p.profile_pic_url ? proxyPic(p.profile_pic_url) : '';
    const avatar = picUrl ? `<img src="${picUrl}" onerror="this.style.display='none'">` : initial;

    c.innerHTML = `
        <div class="profile-avatar">${avatar}</div>
        <div class="profile-info">
            <div class="sp-profile-badges">
                <span class="sp-mini-tag">${escapeHTML(data.page_type_label || 'Profil')}</span>
                <span class="sp-mini-tag sp-muted-tag">${escapeHTML(data.period_label || 'Son 3 Ay')}</span>
            </div>
            <h2>${escapeHTML(p.full_name || p.username || '')}</h2>
            <p>@${escapeHTML(p.username || '')} • ${fmt(data.total_posts_analyzed || p.post_count || 0)} gönderi analiz edildi</p>
        </div>
        <div class="profile-stats">
            <div class="stat-box">
                <div class="stat-val">${fmt(p.followers || 0)}</div>
                <div class="stat-lbl">Takipçi</div>
            </div>
            <div class="stat-box">
                <div class="stat-val">${fmt(data.avg_views || 0)}</div>
                <div class="stat-lbl">Norm. İzlenme</div>
            </div>
            <div class="stat-box">
                <div class="stat-val">%${data.engagement_rate || 0}</div>
                <div class="stat-lbl">Etkileşim</div>
            </div>
        </div>
    `;
}

function renderSponsorshipData(d) {
    const overview = d.overview || {};
    const formatMetrics = d.format_metrics || [];
    const offers = d.offer_packages || d.sponsorship_options || [];
    const maxFormatViews = Math.max(...formatMetrics.map(m => m.avg_views || 0), 1);
    const confidenceScore = overview.confidence_score || 0;

    renderSponsorshipProfile(d);

    document.getElementById('spHeroTitle').textContent = `${d.page_type_label || 'Hesap'} için ${d.period_label || 'seçilen dönem'} teklif modeli`;
    document.getElementById('spHeroDesc').textContent = d.page_type_note || 'Hesap tipi etkisi ve normalize performans birlikte fiyatlanır.';
    document.getElementById('spDriverChips').innerHTML = `
        <span class="sp-chip"><i data-lucide="coins"></i> Referans CPM: ${formatCurrency(d.benchmark_cpm || APP_CONFIG?.default_cpm || 30)}</span>
        <span class="sp-chip"><i data-lucide="message-square-heart"></i> Yorum Kalitesi: ${overview.comment_quality_score || 0}/100</span>
        <span class="sp-chip"><i data-lucide="activity"></i> İstikrar: ${Math.round(overview.consistency_score || 0)}/100</span>
        <span class="sp-chip"><i data-lucide="sparkles"></i> Tip: ${escapeHTML(d.page_type_label || 'Profil')}</span>
    `;
    document.getElementById('spConfidenceCard').innerHTML = `
        <span class="sp-confidence-label">Model Güveni</span>
        <strong>${confidenceScore}/100</strong>
        <p>${getConfidenceLabel(confidenceScore)}</p>
    `;

    const overviewItems = [
        { icon: 'eye', value: fmt(overview.normalized_avg_views || d.avg_views || 0), label: 'Norm. Ortalama İzlenme', tone: 'k-purple' },
        { icon: 'heart', value: fmt(overview.normalized_avg_likes || d.avg_likes || 0), label: 'Norm. Ortalama Beğeni', tone: 'k-green' },
        { icon: 'message-circle', value: fmt(overview.normalized_avg_comments || d.avg_comments || 0), label: 'Norm. Ortalama Yorum', tone: 'k-orange' },
        { icon: 'bar-chart-3', value: `%${overview.engagement_rate || d.engagement_rate || 0}`, label: 'Etkileşim Oranı', tone: 'k-green' },
        { icon: 'message-square-heart', value: `${overview.comment_quality_score || 0}/100`, label: 'Yorum Kalitesi', tone: 'k-orange' },
        { icon: 'shield-check', value: `${confidenceScore}/100`, label: 'Model Güveni', tone: 'k-purple' },
    ];
    document.getElementById('spOverviewGrid').innerHTML = overviewItems.map(item => `
        <div class="kpi-card ${item.tone}">
            <div class="kpi-icon"><i data-lucide="${item.icon}"></i></div>
            <div class="kpi-val">${escapeHTML(item.value)}</div>
            <div class="kpi-lbl">${escapeHTML(item.label)}</div>
        </div>
    `).join('');

    document.getElementById('spFormatMetrics').innerHTML = formatMetrics.map(metric => {
        const width = Math.max(14, Math.round(((metric.avg_views || 0) / maxFormatViews) * 100));
        return `
            <div class="sp-format-row">
                <div class="sp-format-top">
                    <div>
                        <strong>${escapeHTML(metric.label)}</strong>
                        <span>${metric.post_count} içerik • %${metric.share_pct}</span>
                    </div>
                    <div class="sp-format-score">${fmt(metric.avg_views || 0)}</div>
                </div>
                <div class="sp-format-bar"><span style="width:${width}%"></span></div>
                <div class="sp-format-meta">
                    <span><i data-lucide="heart"></i>${fmt(metric.avg_likes || 0)}</span>
                    <span><i data-lucide="message-circle"></i>${fmt(metric.avg_comments || 0)}</span>
                    <span><i data-lucide="bar-chart-2"></i>%${metric.engagement_rate || 0}</span>
                </div>
                ${metric.is_estimated ? '<div class="sp-inline-note">Bu format için veri az olduğu için model genel performanstan türetildi.</div>' : ''}
            </div>
        `;
    }).join('');

    document.getElementById('spInsights').innerHTML = (d.analysis_notes || []).map((note, index) => `
        <div class="strategy-item ${index === 0 ? 's-low' : index === 1 ? 's-med' : 's-high'}">
            <div class="strategy-icon"><i data-lucide="${index === 0 ? 'sparkles' : index === 1 ? 'target' : 'shield-check'}"></i></div>
            <div class="strategy-content"><p>${escapeHTML(note)}</p></div>
        </div>
    `).join('');

    document.getElementById('spOfferSubtext').textContent = `${d.total_posts_analyzed || 0} gönderi ve ${d.period_label || 'seçilen dönem'} verisi ile üretilen teklif bantları.`;

    document.getElementById('spOptions').innerHTML = (offers.length ? offers : [{
        label: 'Uygun format bulunamadı',
        description: 'Bu dönemde hesapta yeterli içerik formatı olmadığı için teklif paketi üretilemedi.',
        format: 'Veri yok',
        integration_type: 'Format yok',
        anchor_price: null,
        estimated_impressions: 0,
        avg_views: 0,
        avg_comments: 0,
        benchmark_cpm: null,
        price_min: null,
        price_max: null,
        confidence_score: 0,
        is_estimated: true,
        isEmptyState: true,
    }]).map(o => `
        <div class="sp-card">
            <div class="sp-card-top">
                <div>
                    <div class="sp-mini-tag-row">
                        <span class="sp-mini-tag">${escapeHTML(o.format)}</span>
                        <span class="sp-mini-tag sp-muted-tag">${escapeHTML(o.integration_type)}</span>
                    </div>
                    <div class="sp-header">
                        <i data-lucide="target" style="color:var(--accent); width:18px; height:18px;"></i>
                        <h4>${escapeHTML(o.label)}</h4>
                    </div>
                </div>
                <div class="sp-anchor-box">
                    <span>Merkez Teklif</span>
                    <strong>${o.anchor_price == null ? '—' : formatCurrency(o.anchor_price)}</strong>
                </div>
            </div>
            <p class="sp-desc">${escapeHTML(o.description)}</p>
            <div class="sp-metrics">
                <div class="sp-metric-row">
                    <span class="sp-metric-lbl">Tahmini Gösterim</span>
                    <span class="sp-metric-val">${fmt(o.estimated_impressions || 0)}</span>
                </div>
                <div class="sp-metric-row">
                    <span class="sp-metric-lbl">Baz İzlenme</span>
                    <span class="sp-metric-val">${fmt(o.avg_views || 0)}</span>
                </div>
                <div class="sp-metric-row">
                    <span class="sp-metric-lbl">Baz Yorum</span>
                    <span class="sp-metric-val">${fmt(o.avg_comments || 0)}</span>
                </div>
                <div class="sp-metric-row">
                    <span class="sp-metric-lbl">Paket Benchmark CPM</span>
                    <span class="sp-metric-val">${formatCurrency(o.benchmark_cpm)}</span>
                </div>
            </div>
            <div class="sp-price-tag">
                <span>Önerilen Teklif Aralığı</span>
                <strong>${o.price_min == null ? 'Format verisi bekleniyor' : `${formatCurrency(o.price_min)} — ${formatCurrency(o.price_max)}`}</strong>
            </div>
            <div class="sp-card-footer">
                <span><i data-lucide="gauge"></i> Güven ${o.confidence_score || 0}/100</span>
                <span><i data-lucide="layers-3"></i> ${o.isEmptyState ? 'Teklif üretilemedi' : (o.is_estimated ? 'Tahmini model' : 'Doğrudan format verisi')}</span>
            </div>
        </div>
    `).join('');
}

/* =======================================
   X. INFO MODAL (BİLGİ PENCERESİ)
   ======================================= */
function openInfoModal(type) {
    const titleEl = document.getElementById('infoModalTitle');
    const contentEl = document.getElementById('infoModalContent');
    
    let title = '';
    let content = '';

    if (type === 'dashboard') {
        title = 'Genel Bakış Analizi Nasıl Çalışır?';
        content = `
            <p style="margin-bottom:12px;"><strong>Genel Bakış</strong>, seçilen platformda erişilebilen profil ve içerik metriklerini gösterir. Instagram oturum ister; TikTok ve YouTube herkese açık kaynakları kullanır.</p>
            <ul style="padding-left:20px; margin-bottom:12px; display:flex; flex-direction:column; gap:6px;">
                <li><strong>Veri Çekimi:</strong> Kaynağın sunduğu profil, takipçi/abone ve içerik metrikleri alınır. Veri kapsamı, kaynak ve eksik metrik notları sonuçların üzerinde gösterilir.</li>
                <li><strong>Etkileşim Oranı:</strong> (Ortalama Beğeni + Ortalama Yorum) / Takipçi Sayısı * 100 formülüyle hesaplanır. Çekilen son paylaşımlar üzerinden ortalama alınır.</li>
                <li><strong>İzlenme Verisi:</strong> Kaynağın sunduğu video/Reels/Shorts izlenme sayıları gösterilir. Eksik metrikler sıfır yerine boş bırakılır.</li>
                <li><strong>Limitler:</strong> İçerik sayısı ve dönem kapsamı platformun döndürdüğü veriye bağlıdır. Sayı sınırı veya kısmi örneklem sonuç notlarında belirtilir. "Gizli" hesapların verileri çekilemez.</li>
            </ul>
            <p><strong>Kullanım Amacı:</strong> Bir rakibin veya influencer'ın genel sağlık durumunu (engagement rate, ortalama like/view) hızlıca görmek içindir.</p>
        `;
    } 
    else if (type === 'compare') {
        title = 'Karşılaştırma Mantığı Nasıl Çalışır?';
        content = `
            <p style="margin-bottom:12px;"><strong>Hesap Karşılaştırma</strong>, birden fazla hesabı teknik metriklerle yan yana koyarak "kıyaslamalı performans (benchmarking)" analizi yapar.</p>
            <ul style="padding-left:20px; margin-bottom:12px; display:flex; flex-direction:column; gap:6px;">
                <li><strong>Normalize Performans (Radar Chart):</strong> Mevcut metrikler, eklenen hesaplar içindeki en yüksek değere göre 0-100 aralığında karşılaştırılır. Eksik metrikler puana sıfır olarak eklenmez; mevcut ağırlıklarla değerlendirilir. Örneğin, biri 100k, diğeri 50k takipçiliyse biri 100, diğeri 50 puan alır. Bu, hesapların güçlü ve zayıf yönlerini şekilsel olarak gösterir.</li>
                <li><strong>İçerik Dağılımı:</strong> Hesapların hangi formatta (Reels, Video, Shorts, Fotoğraf, Carousel veya Slayt) paylaşım yaptığı %100'e tamamlanacak şekilde hesaplanır.</li>
                <li><strong>Teknik Analiz Yorumu:</strong> Sistem algoritmaları, hesaplar arası metrik farklarını analiz eder. Örneğin; az takipçisi olup çok beğeni alan hesaplar için "Takipçi Verimliliği Yüksek" yorumunu otomatik üretir.</li>
            </ul>
        `;
    }
    else if (type === 'sponsorship') {
        title = 'Sponsorluk Değerlemesi Nedir?';
        content = `
            <p style="margin-bottom:12px;"><strong>Sponsorluk Değerleme Motoru</strong>, hesabın son dönemdeki temizlenmiş performansını ve hesap tipinin ikna etkisini birlikte fiyatlar.</p>
            <ul style="padding-left:20px; margin-bottom:12px; display:flex; flex-direction:column; gap:6px;">
                <li><strong>Referans CPM:</strong> Sistem, yapılandırmadaki referans CPM değerini başlangıç noktası olarak alır. Bu bir tahmin modelidir; piyasa teklifi veya garanti değildir.</li>
                <li><strong>Normalize Performans:</strong> Aşırı viral içerikler filtrelenir; izlenme, beğeni ve yorum ortalamaları temizlenmiş veriyle hesaplanır.</li>
                <li><strong>Hesap Tipi Etkisi:</strong> Influencer, meme, topluluk veya yerel sayfa gibi tipler aynı erişimi farklı güven ve dönüşüm gücüyle taşır; fiyat buna göre değişir.</li>
                <li><strong>Yorum Kalitesi Formülü:</strong> Skor; derinlik, özgünlük, bağlam uyumu, soru / niyet sinyali, duygu dengesi ve spam sağlığı başlıklarını ağırlıklı puanlar.</li>
                <li><strong>Format Kuralı:</strong> Hesapta görünmeyen formatlar için paket üretilmez; örneğin reels yoksa reels ve ona bağlı türev öneriler listelenmez.</li>
                <li><strong>Paket Bazlı Fiyat:</strong> Tek görsel, carousel, reels ve uygun olduğunda UGC benzeri işlerin her biri ayrı çarpanlarla dar bir teklif aralığı üretir.</li>
            </ul>
        `;
    }
    titleEl.innerText = title;
    contentEl.innerHTML = content;
    document.getElementById('infoModalOverlay').classList.add('active');
}

function closeInfoModal() {
    document.getElementById('infoModalOverlay').classList.remove('active');
}

/* =======================================
   4. İÇERİK DNA ANALİZİ
   ======================================= */
document.getElementById('dnaInput')?.addEventListener('keydown', e => { if (e.key === 'Enter') fetchContentDNA(); });

async function fetchContentDNA() {
    if (activePlatform !== 'instagram') { toast('İçerik DNA yalnız Instagram için kullanılabilir.'); return; }
    if (!APP_CONFIG?.ai_enabled) { toast('İçerik DNA için kendi Gemini API anahtarınızı ekleyin.'); return; }
    const input = document.getElementById('dnaInput').value.trim();
    const timeRange = document.getElementById('dnaTimeRange')?.value || '90';
    if (!input) { showError('dnaErrorBox', 'dnaErrorText', 'Lütfen bir hesap adı girin.'); return; }

    showEl('dnaErrorBox', false);
    showEl('dnaResults', false);
    showEl('dnaLoading', true);
    document.getElementById('dnaFetchBtn').disabled = true;

    try {
        const res = await fetch('/api/content_dna', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ input, timeRange })
        });
        const d = await res.json();
        if (!res.ok) {
            showError('dnaErrorBox', 'dnaErrorText', d.error || 'Hata oluştu');
            return;
        }
        
        const formatNumber = n => n == null ? 'Mevcut değil' : new Intl.NumberFormat('tr-TR', { notation: "compact", compactDisplay: "short" }).format(n);
        const rankingNote = document.getElementById('dnaRankingNote');
        rankingNote.textContent = d.ranking_note || 'Sıralama, mevcut performans metrikleri ve seçilen örneklem üzerinden yapılır.';
        
        const renderPostCard = (post) => {
            const postUrl = safeInstagramUrl(`https://instagram.com/p/${encodeURIComponent(post.shortcode)}/`);
            return `
                <div style="display: flex; gap: 20px; padding: 16px; background: var(--surface-hover); border-radius: 12px; align-items: flex-start;">
                    <div style="flex-shrink: 0; width: 120px; height: 120px; border-radius: 8px; overflow: hidden; background: rgba(0,0,0,0.05);">
                        ${post.thumbnail_url ? `<img src="${proxyPic(post.thumbnail_url)}" style="width: 100%; height: 100%; object-fit: cover;" onerror="this.style.display='none'">` : `<div style="width:100%; height:100%; display:flex; align-items:center; justify-content:center; color:#999;"><i data-lucide="image"></i></div>`}
                    </div>
                    <div style="flex-grow: 1;">
                        <div style="display: flex; gap: 16px; margin-bottom: 12px; font-size: 13px; color: var(--text-muted);">
                            <span><i data-lucide="heart" style="width:14px;height:14px;vertical-align:middle;margin-right:4px;"></i>${formatNumber(post.likes)}</span>
                            <span><i data-lucide="message-circle" style="width:14px;height:14px;vertical-align:middle;margin-right:4px;"></i>${formatNumber(post.comments_count)}</span>
                            <span><i data-lucide="eye" style="width:14px;height:14px;vertical-align:middle;margin-right:4px;"></i>${formatNumber(post.views)}</span>
                            <a href="${postUrl}" target="_blank" style="color: var(--primary); text-decoration: none; margin-left: auto;">
                                Gönderiye Git <i data-lucide="external-link" style="width:14px;height:14px;vertical-align:middle;"></i>
                            </a>
                        </div>
                        <div style="margin-bottom: 12px; font-size: 14px; color: var(--text-primary); line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;">
                            ${escapeHTML(post.caption || 'Açıklama yok')}
                        </div>
                        <div style="padding: 12px; background: rgba(0,0,0,0.03); border-radius: 8px; border-left: 3px solid var(--primary); font-size: 14px; color: var(--text-secondary); line-height: 1.6;">
                            <strong style="color:var(--text-primary);">AI Analizi:</strong> ${escapeHTML(post.ai_analysis)}
                        </div>
                    </div>
                </div>
            `;
        };

        const bestPostsHtml = (d.best_posts || []).map(renderPostCard).join('');
        const worstPostsHtml = (d.worst_posts || []).map(renderPostCard).join('');

        document.getElementById('dnaBestPostsList').innerHTML = bestPostsHtml || '<div style="color:var(--text-muted);">Veri bulunamadı.</div>';
        document.getElementById('dnaWorstPostsList').innerHTML = worstPostsHtml || '<div style="color:var(--text-muted);">Veri bulunamadı.</div>';
        document.getElementById('dnaOverallStrategy').textContent = d.overall_strategy || 'Veri bulunamadı.';
        
        showEl('dnaResults', true);
        toast('İçerik DNA analizi tamamlandı.');
    } catch (e) {
        showError('dnaErrorBox', 'dnaErrorText', 'Bağlantı hatası: ' + e.message);
    } finally {
        showEl('dnaLoading', false);
        document.getElementById('dnaFetchBtn').disabled = false;
        window.lucide?.createIcons?.();
    }
}

