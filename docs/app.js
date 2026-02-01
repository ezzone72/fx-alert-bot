/* =========================================================
   FX Alert PWA - Push registration (PAGE SCRIPT ONLY)
   - IMPORTANT: DO NOT register service worker here.
   - Service worker is registered in index.html as:
     navigator.serviceWorker.register("/fx-alert-bot/sw.js", { scope: "/fx-alert-bot/" })
   ========================================================= */

/* ===== 1) Firebase 설정 (이미 넣어두신 값 유지) ===== */
// Your web app's Firebase configuration
const firebaseConfig = {
  apiKey: "AIzaSyAGpSq-UbQW3HLWBPsYC8q0tz3DQPNBIjI",
  authDomain: "fx-alert-73663.firebaseapp.com",
  projectId: "fx-alert-73663",
  storageBucket: "fx-alert-73663.firebasestorage.app",
  messagingSenderId: "4733193042",
  appId: "1:4733193042:web:7c3853ed9e77de97fd488b"
};

/* ===== 2) VAPID 공개 키 (이미 넣어두신 값 유지) ===== */
const VAPID_KEY = "BAeVrDv83RYpmoNSqlvUpgI9banBADRnOiu44Mqnq90Q4cr1O04t-ONRmDBtFsVorZe3a9CCLLaQi4UWAohIbLc";

/* ===== 상태 표시 ===== */
function setStatus(msg, ok = true) {
  const el = document.getElementById("status");
  if (!el) return;
  el.textContent = `상태: ${msg}`;
  el.style.color = ok ? "#7CFF7C" : "#FF7C7C";
}

function setTokenPreview(token) {
  const el = document.getElementById("tokenPreview");
  if (!el) return;
  el.textContent = token ? (token.slice(0, 12) + "..." + token.slice(-8)) : "-";
}

/* ===== 메인: 푸시 등록 ===== */
async function registerPush() {
  try {
    setStatus("초기화 중...");

    // Firebase SDK가 먼저 로드되어 있어야 합니다.
    if (!window.firebase) throw new Error("firebase SDK not loaded");

    firebase.initializeApp(firebaseConfig);
    const messaging = firebase.messaging();
    const db = firebase.firestore();

    // SW가 준비될 때까지 대기 (index.html에서 등록한 sw.js 사용)
    if (!("serviceWorker" in navigator)) {
      throw new Error("ServiceWorker not supported");
    }
    setStatus("Service Worker 준비 대기...");
    const reg = await navigator.serviceWorker.ready;

    // 알림 권한
    setStatus("알림 권한 요청 중...");
    const perm = await Notification.requestPermission();
    if (perm !== "granted") {
      setStatus("알림 권한이 거부되었습니다", false);
      return;
    }

    // 토큰 발급
    setStatus("푸시 토큰 발급 중...");
    const token = await messaging.getToken({
      vapidKey: VAPID_KEY,
      serviceWorkerRegistration: reg
    });

    if (!token) throw new Error("token is empty");
    console.log("FCM TOKEN:", token);
    setTokenPreview(token);

    // Firestore 저장
    setStatus("Firestore에 저장 중...");
    await db.collection("subscribers").doc(token).set({
      token,
      ua: navigator.userAgent,
      createdAt: new Date().toISOString()
    }, { merge: true });

    setStatus("등록 완료 ✅ 이제 푸시를 받을 수 있습니다!");
  } catch (e) {
    console.error(e);
    setStatus(`오류: ${e.message}`, false);
  }
}

/* ===== 버튼 연결 ===== */
document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("btn");
  if (btn) btn.addEventListener("click", registerPush);
});
