document.querySelectorAll('.language-switch a').forEach(link => { link.hash = location.hash; });
window.addEventListener('hashchange', () => document.querySelectorAll('.language-switch a').forEach(link => { link.hash = location.hash; }));
