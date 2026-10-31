// Public settings (none of these are secrets).
window.NAMESTORAGE_CONFIG = {
  // Google Cloud Console > APIs & Services > Credentials > OAuth client ID (Web application)
  googleClientId: "406336099577-p39shln7qk6prfhrrvlhmkm5mf3kf93d.apps.googleusercontent.com",
  // Where the signaling server is reachable. Use wss:// in production.
  signalingUrl: "wss://piyush.tail799736.ts.net",
  iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
};
