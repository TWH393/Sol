// Runs before styles load. Local storage is a paint cache; app.theme is authoritative.
(() => {
  const apply = value => {
    const theme = value === 'dark' ? 'dark' : 'light';
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="color-scheme"]')?.setAttribute('content', theme);
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'light' ? '#FAF8F3' : '#212121');
    const button = document.getElementById('toggle-theme');
    if (button) {
      button.textContent = theme === 'light' ? '切换暗色' : '切换浅色';
      button.setAttribute('aria-label', button.textContent);
    }
    const favicon = document.getElementById('theme-favicon');
    if (favicon) favicon.href = theme === 'light' ? '/brand/sol-main.ico' : '/brand/sol-dark.ico';
    try { localStorage.setItem('sol.theme', theme); } catch {}
    return theme;
  };
  let cached;
  try { cached = localStorage.getItem('sol.theme'); } catch {}
  window.solTheme = { apply, current: () => document.documentElement.dataset.theme || 'light' };
  const requested=new URLSearchParams(location.search).get('theme');
  apply(requested==='dark'||requested==='light'?requested:cached);
})();
