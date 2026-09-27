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
  document.documentElement.style.visibility = 'hidden';
  _fetch('/api/session').then(r => r.json()).then(d => {
    if(!d.logged_in){ toLogin(); return; }
    window.SD_SESSION = d;
    document.documentElement.style.visibility = '';
    document.dispatchEvent(new CustomEvent('sd-session', {detail: d}));
  }).catch(() => { document.documentElement.style.visibility = ''; });
})();
