// Public settings (none of these are secrets).
window.NAMESTORAGE_CONFIG = {
  // Google Cloud Console > APIs & Services > Credentials > OAuth client ID (Web application)
  googleClientId: "YOUR_CLIENT_ID.apps.googleusercontent.com",
  // Where the signaling server is reachable. Use wss:// in production.
  signalingUrl: "wss://signal.example.com/",
  iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
};
