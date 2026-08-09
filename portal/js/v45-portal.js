(() => {
  const cfg = window.YUSHE_V45 || {};
  const inferred = location.protocol === "file:" ? "http://127.0.0.1:8000" : location.origin;
  const API = (cfg.API_BASE || inferred).replace(/\/$/,"");
  const KEY = "yushe_v45_session";
  let session = JSON.parse(localStorage.getItem(KEY) || "null");
  let activeOrder = null;
  let pollTimer = null;
  let pollStarted = 0;

  const $ = id => document.getElementById(id);
  function toast(msg){ const el=$("toast"); el.textContent=msg; el.hidden=false; setTimeout(()=>el.hidden=true,4200); }
  async function api(path, options={}){
    const res = await fetch(API+path, options);
    let data={}; try{ data=await res.json(); }catch{}
    if(!res.ok) throw new Error(data.detail || data.message || `HTTP ${res.status}`);
    return data;
  }
  function setApi(ok, text){ const e=$("apiState"); e.textContent=text; e.className="api-state "+(ok?"ok":"bad"); }
  function setAuthUI(){
    const logged=!!session?.token;
    $("logoutBtn").hidden=!logged;
    $("loginBtn").hidden=logged;
    $("email").disabled=logged;
    $("accountLabel").textContent=logged ? (session.account_id || "已登入") : "尚未登入";
    if(!logged) $("points").textContent="—";
  }
  async function health(){
    try{
      const d=await api("/billing/health");
      if(d.version!=="4.5.0") setApi(false,`Backend ${d.version || "?"}，不是 v4.5.0`);
      else if(!d.line_pay_configured) setApi(false,"v4.5 正常，但 LINE Pay 尚未設定金鑰");
      else setApi(true,"v4.5 · LINE Pay 已設定");
    }catch(e){ setApi(false,"Backend 無法連線"); }
  }
  async function login(){
    const email=$("email").value.trim();
    if(!email){ toast("請輸入 Email"); return; }
    try{
      const d=await api("/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({account_type:"email",account_id:email})});
      session={token:d.access_token,account_id:d.user.account_id};
      localStorage.setItem(KEY,JSON.stringify(session));
      $("points").textContent=Number(d.user.points||0).toLocaleString();
      setAuthUI();
      await Promise.all([loadPlans(),loadOrders(),refreshStatus()]);
      toast("登入成功");
    }catch(e){ toast("登入失敗："+e.message); }
  }
  function logout(){ session=null; localStorage.removeItem(KEY); stopPoll(); setAuthUI(); $("ordersBody").innerHTML='<tr><td colspan="5">登入後顯示</td></tr>'; toast("已登出"); }
  async function refreshStatus(){
    if(!session?.token) return;
    try{
      const d=await api("/trial/status?token="+encodeURIComponent(session.token));
      if(!d.logged_in){ logout(); return; }
      $("points").textContent=Number(d.points||0).toLocaleString();
      $("accountLabel").textContent=d.account_id||session.account_id;
    }catch(e){}
  }
  async function loadPlans(){
    try{
      const d=await api("/billing/plans");
      const plans=d.plans||{};
      $("plans").innerHTML=Object.entries(plans).map(([id,p])=>`
        <article class="plan">
          <h3>${escapeHtml(p.name)}</h3>
          <div class="pts">${Number(p.points).toLocaleString()} 點</div>
          <div class="price">NT$ ${Number(p.amount).toLocaleString()}</div>
          <button class="primary" data-plan="${escapeAttr(id)}">LINE Pay 儲值</button>
        </article>`).join("");
      document.querySelectorAll("[data-plan]").forEach(btn=>btn.addEventListener("click",()=>createOrder(btn.dataset.plan)));
    }catch(e){ $("plans").innerHTML='<div class="skeleton">方案讀取失敗：'+escapeHtml(e.message)+'</div>'; }
  }
  async function createOrder(planId){
    if(!session?.token){ toast("請先登入"); return; }
    try{
      $("paymentPanel").hidden=false;
      $("paymentStatus").className="status waiting"; $("paymentStatus").textContent="正在建立 LINE Pay 訂單…";
      const d=await api("/billing/create_order",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:session.token,plan_id:planId})});
      activeOrder=d;
      $("qr").src=d.payment_qr;
      $("payLink").href=d.payment_url;
      $("orderId").textContent=d.order_id;
      $("orderPlan").textContent=d.plan_name;
      $("orderAmount").textContent=`NT$ ${Number(d.amount).toLocaleString()}`;
      $("orderPoints").textContent=`${Number(d.points).toLocaleString()} 點`;
      $("paymentStatus").textContent="等待 LINE Pay 實際付款";
      startPoll();
    }catch(e){
      $("paymentStatus").className="status error"; $("paymentStatus").textContent="建立訂單失敗";
      toast(e.message);
    }
  }
  function stopPoll(){ if(pollTimer){clearInterval(pollTimer);pollTimer=null;} }
  function startPoll(){
    stopPoll(); pollStarted=Date.now();
    pollTimer=setInterval(async()=>{
      if(!activeOrder || !session?.token) return;
      if(Date.now()-pollStarted > (cfg.POLL_TIMEOUT_MS||300000)){ stopPoll(); toast("付款狀態等待逾時，可重新整理訂單狀態"); return; }
      try{
        const d=await api("/billing/order_status?token="+encodeURIComponent(session.token)+"&order_id="+encodeURIComponent(activeOrder.order_id));
        if(d.status==="paid"){
          stopPoll();
          $("paymentStatus").className="status paid"; $("paymentStatus").textContent="付款成功・已自動入點";
          await Promise.all([refreshStatus(),loadOrders()]);
          toast("LINE Pay 付款確認成功，點數已入帳");
        }else if(d.status==="cancelled"){
          stopPoll(); $("paymentStatus").className="status error"; $("paymentStatus").textContent="付款已取消";
        }else{
          $("paymentStatus").className="status waiting"; $("paymentStatus").textContent="等待 LINE Pay 實際付款";
        }
      }catch(e){
        if(String(e.message).includes("LOGIN_REQUIRED")) logout();
      }
    },cfg.POLL_MS||2000);
  }
  async function loadOrders(){
    if(!session?.token) return;
    try{
      const d=await api("/billing/orders?token="+encodeURIComponent(session.token)+"&limit=30");
      const items=d.items||[];
      $("ordersBody").innerHTML=items.length?items.map(o=>`<tr>
        <td>${escapeHtml(o.order_id)}</td><td>${escapeHtml(o.plan_name)}</td>
        <td>NT$ ${Number(o.amount).toLocaleString()}</td><td>${Number(o.points).toLocaleString()}</td>
        <td>${statusText(o.status)}</td></tr>`).join(""):'<tr><td colspan="5">尚無儲值訂單</td></tr>';
    }catch(e){ if(String(e.message).includes("LOGIN_REQUIRED")) logout(); }
  }
  function statusText(v){ return ({paid:"已付款入點",payment_requested:"等待付款",created:"已建立",cancelled:"已取消",failed:"失敗"})[v]||v||"—"; }
  function escapeHtml(s){return String(s??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m]));}
  function escapeAttr(s){return escapeHtml(s);}
  $("loginBtn").addEventListener("click",login);
  $("logoutBtn").addEventListener("click",logout);
  $("refreshBtn").addEventListener("click",()=>Promise.all([refreshStatus(),loadOrders(),health()]));
  $("email").addEventListener("keydown",e=>{if(e.key==="Enter")login();});
  setAuthUI(); health(); loadPlans();
  if(session?.token){ $("email").value=session.account_id||""; refreshStatus(); loadOrders(); }
})();