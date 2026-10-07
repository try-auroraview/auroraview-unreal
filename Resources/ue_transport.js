// AuroraView Unreal host transport. Core owns RPC IDs, promises and event delivery.
// Install only on an owned, trusted top-level document after BindUObject is ready.
(function () {
  'use strict';
  window.__auroraviewInstallUETransport = function (token) {
    if (window.top !== window) return false;
    var endpoint = window.ue && window.ue.auroraview;
    if (!endpoint || typeof endpoint.postmessage !== 'function' || typeof endpoint.markready !== 'function') return false;
    window.ipc = {
      postMessage: function (payload) {
        if (typeof payload !== 'string') throw new TypeError('AuroraView IPC requires JSON text');
        var message = JSON.parse(payload);
        if (message.type === 'event' && message.event === '__auroraview_ready') {
          function failedReady(error) {
            // This error is meaningful even without a request ID. Core cancels
            // pending calls on a fatal backend error and fails later calls fast.
            if (window.auroraview && window.auroraview.trigger) {
              window.auroraview.trigger('backend_error', {
                message: 'connection lost: Unreal ready handshake rejected: ' + String(error)
              });
            }
          }
          try {
            Promise.resolve(endpoint.markready(token)).then(function (accepted) {
              if (accepted !== true) failedReady('session closed or stale');
            }, failedReady);
          } catch (error) {
            failedReady(error);
          }
          return;
        }
        function reject(error) {
          if (message.id && window.auroraview && window.auroraview.trigger) {
            window.auroraview.trigger(message.type === 'invoke' ? '__invoke_result__' : '__auroraview_call_result', {
              id: message.id, ok: false,
              error: { name: 'TransportError', message: String(error), code: 'TRANSPORT_REJECTED' }
            });
          }
        }
        // BindUObject returns asynchronous JS futures. Do not mistake it for
        // the host handler's result; actual results use the upstream event.
        Promise.resolve(endpoint.postmessage(payload, token)).then(function (accepted) {
          if (accepted !== true) reject('Unreal session closed, stale, or queue capacity exceeded');
        }, reject);
      }
    };
    return true;
  };
})();
