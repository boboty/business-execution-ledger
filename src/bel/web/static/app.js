/* BEL Phase 2C — minimal native JS. The only dynamic behavior is the
   manual InvoiceItem allocation POST; everything else (period dropdown,
   decision traces, allocation form toggle) is native HTML. No inline
   script, no third-party CDN (CSP: script-src 'self'). */
(function () {
  "use strict";

  function showError(form, message) {
    var box = form.querySelector("[data-allocation-error]");
    if (box) {
      box.textContent = message;
      box.hidden = false;
    }
  }

  document.querySelectorAll("form[data-allocation-submit]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var payload = {};
      new FormData(form).forEach(function (value, key) {
        payload[key] = value;
      });
      fetch("/api/invoice-item-allocations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      })
        .then(function (response) {
          if (response.ok) {
            window.location.reload();
            return null;
          }
          return response.json().then(function (data) {
            showError(form, (data && data.detail) || "关联失败");
          });
        })
        .catch(function () {
          showError(form, "请求失败，请重试");
        });
    });
  });
})();
