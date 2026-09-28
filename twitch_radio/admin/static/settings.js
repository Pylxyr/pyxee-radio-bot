
(function () {
  // Local uptime ticker — the "up Xh Ym" chip only needs to look alive,
  // not be pushed from the server every second for that.
  var uptimeEl = document.getElementById("chip-uptime");
  if (uptimeEl) {
    var base = parseInt(uptimeEl.dataset.uptimeBase || "0", 10);
    var start = performance.now();
    setInterval(function () {
      var total = base + Math.floor((performance.now() - start) / 1000);
      var h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60);
      uptimeEl.textContent = h ? ("up " + h + "h " + m + "m") : ("up " + m + "m");
    }, 1000);
  }
})();
