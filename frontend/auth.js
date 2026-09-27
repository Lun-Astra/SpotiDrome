// Shared login gate, loaded first by every page. The backend refuses every
// /api call without a session (401 + login_required), so this sends anyone
// who isn't logged in to /login.html, and keeps the page hidden until the
// session check has answered so nothing flashes before the redirect.
(function(){
  const _fetch = window.fetch.bind(window);
  function toLogin(){
    location.href = '/login.html?next=' + encodeURIComponent(location.pathname + location.search);
  }
  // Any API call that comes back "login required" (session expired, logged
  // out in another tab) sends the page to the login screen. Other 401s - e.g.
  // Spotify needing a reconnect - carry no login_required and pass through.
  window.fetch = (url, opts) => _fetch(url, opts).then(r => {
    if(r.status === 401 && typeof url === 'string' && url.startsWith('/api/') && !url.startsWith('/api/session')){
      r.clone().json().then(d => { if(d && d.login_required) toLogin(); }).catch(() => {});
    }
    return r;
  });
  window.sdLogout = async () => {
    try{ await _fetch('/api/session/logout', {method: 'POST'}); }catch(e){}
    toLogin();
  };
  // If the API itself can't be reached (e.g. a reverse proxy blocking /api/),
  // say so instead of showing a page full of empty lists.
  function showApiError(msg){
    document.documentElement.style.visibility = '';
    const bar = document.createElement('div');
    bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9999;background:#7f1d1d;color:#fff;' +
      'font:13px/1.5 sans-serif;padding:10px 16px;text-align:center';
    bar.textContent = msg;
    (document.body || document.documentElement).appendChild(bar);
  }
  document.documentElement.style.visibility = 'hidden';
  _fetch('/api/session').then(async r => {
    const d = await r.json().catch(() => null);
    if(!d){
      showApiError(`Can't reach the SpotiDrome API: /api/session answered HTTP ${r.status}` +
        (r.status === 403 ? ' from the reverse proxy - it has to let /api/ through to SpotiDrome.' : '.'));
      return;
    }
    if(!d.logged_in){ toLogin(); return; }
    window.SD_SESSION = d;
    document.documentElement.style.visibility = '';
    document.dispatchEvent(new CustomEvent('sd-session', {detail: d}));
  }).catch(() => showApiError("Can't reach the SpotiDrome API (network error)."));
})();
