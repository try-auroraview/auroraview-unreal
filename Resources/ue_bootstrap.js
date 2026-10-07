// Compatibility boundary for the pinned upstream startup stub. Keep vendored
// Core files unchanged; remove these two adaptations after the shared fixes land.
(function () {
  'use strict';
  var stub = window.auroraview;
  if (!stub || !stub._isStub) return;

  // The pinned Core replays early invoke as call. This Editor host intentionally
  // does not implement invoke, so reject before it can enter the replay queue.
  stub.invoke = function () {
    var error = new Error('This host accepts auroraview.call only');
    error.name = 'NotSupportedError';
    error.code = 'UNSUPPORTED';
    return Promise.reject(error);
  };

  // The pinned stub loses whenReady resolvers when Core replaces it. Listen to
  // Core's existing ready event without duplicating its call or event machinery.
  stub.whenReady = function () {
    return new Promise(function (resolve, reject) {
      function cleanup() {
        window.removeEventListener('auroraviewready', ready);
        window.removeEventListener('beforeunload', unloaded);
      }
      function ready() {
        if (!window.auroraview || !window.auroraview._ready) return;
        cleanup();
        resolve(window.auroraview);
      }
      function unloaded() {
        cleanup();
        var error = new Error('AuroraView closed before ready');
        error.name = 'CancelledError';
        error.code = 'CANCELLED';
        reject(error);
      }
      window.addEventListener('auroraviewready', ready);
      window.addEventListener('beforeunload', unloaded);
      ready();
    });
  };
})();
