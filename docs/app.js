/* =========================================================
   FX Alert PWA - Firebase Push Registration
   ========================================================= */

/* ===== 1. Firebase 설정 (콘솔에서 복사한 값으로 교체) ===== */
// Your web app's Firebase configuration
const firebaseConfig = {
  apiKey: "AIzaSyAGpSq-UbQW3HLWBPsYC8q0tz3DQPNBIjI",
  authDomain: "fx-alert-73663.firebaseapp.com",
  projectId: "fx-alert-73663",
  storageBucket: "fx-alert-73663.firebasestorage.app",
  messagingSenderId: "4733193042",
  appId: "1:4733193042:web:7c3853ed9e77de97fd488b"
};

/* ===== 2. VAPID 공개 키 (Cloud Messaging에서 생성) ===== */
const VAPID_KEY = "BAeVrDv83RYpmoNSqlvUpgI9banBADRnOiu44Mqnq90Q4cr1O04t-ONRmDBtFsVorZe3a9CCLLaQi4UWAohIbLc";

/* =========================================================
   Firebase 초기화
   ========================================================= */
firebase.initializeApp(firebaseConfig);

const messaging = firebase.messaging();
const db = firebase.firestore();

/* =========================================================
   Service Worker 등록
   ========================================================= */
async function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) {
    throw new Error("Service Worker not supported");
  }
  const reg = await navigator.serviceWorker.register("./app.js");
  return reg;
}

/* =========================================================
   상태 표시 유틸
   ========================================================= */
function setStatus(msg, ok = true) {
  const el = document.getElementById("status");
  if (!el) return;
  el.textContent = `상태: ${msg}`;
  el.style.color = ok ? "#7CFF7C" : "#FF7C7C";
}

/* =========================================================
   푸시 등록 메인 로직
   ========================================================= */
async function registerPush() {
  try {
    setStatus("Service Worker 등록 중...");
    const reg = await registerServiceWorker();

    setStatus("알림 권한 요청 중...");
    const perm = await Notification.requestPermission();
    if (perm !== "granted") {
      setStatus("알림 권한이 거부되었습니다", false);
      return;
    }

    setStatus("푸시 토큰 발급 중...");
    const token = await messaging.getToken({
      vapidKey: VAPID_KEY,
      serviceWorkerRegistration: reg
    });

    if (!token) {
      setStatus("토큰 발급 실패", false);
      return;
    }

    console.log("FCM TOKEN:", token);

    setStatus("Firestore에 토큰 저장 중...");
    await db.collection("subscribers").doc(token).set({
      token: token,
      ua: navigator.userAgent,
      createdAt: new Date().toISOString()
    }, { merge: true });

    setStatus("등록 완료 ✅ 이제 푸시를 받을 수 있습니다!");

    const preview = document.getElementById("tokenPreview");
    if (preview) {
      preview.textContent = token.slice(0, 12) + "..." + token.slice(-8);
    }

  } catch (e) {
    console.error(e);
    setStatus("오류: " + e.message, false);
  }
}

/* =========================================================
   버튼 연결
   ========================================================= */
document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("btn");
  if (btn) {
    btn.addEventListener("click", registerPush);
  }
});
