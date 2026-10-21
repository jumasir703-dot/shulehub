// Adds the CSRF token to every POST form and every fetch() POST, so the
// older pages (timetable builder, onboarding...) work with the login gate
// without each template having to know about it.
(function () {
  var meta = document.querySelector('meta[name="csrf-token"]');
  if (!meta) return;
  var token = meta.getAttribute('content');

  function stamp(form) {
    if ((form.getAttribute('method') || 'get').toLowerCase() !== 'post') return;
    if (form.querySelector('input[name="_csrf"]')) return;
    var i = document.createElement('input');
    i.type = 'hidden'; i.name = '_csrf'; i.value = token;
    form.appendChild(i);
  }
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('form').forEach(stamp);
  });
  document.addEventListener('submit', function (e) { stamp(e.target); }, true);

  var realFetch = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    var method = (init.method || (input && input.method) || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
      var h = new Headers(init.headers || (input && input.headers) || {});
      if (!h.has('X-CSRF-Token')) h.set('X-CSRF-Token', token);
      init.headers = h;
    }
    return realFetch.call(this, input, init);
  };
})();
