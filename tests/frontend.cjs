// Offline logic tests. No requests to a bank, CRM or iiko.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../static/kiosk-preview.html'),'utf8');
const code=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const nodes={modalRoot:{innerHTML:''}};
const c=vm.createContext({console,URLSearchParams,URL,window:{location:{pathname:'/Aray',search:''}},localStorage:{getItem(){return null},setItem(){}},sessionStorage:{setItem(){},removeItem(){}},document:{getElementById:id=>nodes[id]||null},clearTimeout(){}});
vm.runInContext(code.slice(0,code.lastIndexOf('enableGlobalMenuWheel();')),c);
const run=s=>vm.runInContext(s,c);
run(`products=[{id:'p',itemId:'p',price:1000,modifierGroups:[{id:'service',name:'Тип заказа',items:[{id:'hall',name:'- зале',price:0},{id:'take',name:'- с собой',price:50}]},{id:'extras',name:'Добавки',minQuantity:1,items:[{id:'cheese',name:'Сыр',price:200}]}]}];orderMode='takeaway';cart=[{productId:'p',q:1,unitPrice:1200,modifierIds:['hall','cheese'],modifierGroupIds:['service','extras'],mods:['- зале','Сыр']}];`);
run('applyOrderModeToExistingCart()');
assert.equal(run('cart[0].unitPrice'),1250);
assert.equal(run('cart[0].modifierIds.join()'),'cheese,take');
run("orderMode='dinein';applyOrderModeToExistingCart()");
assert.equal(run('cart[0].unitPrice'),1200);
assert.equal(run('visibleModifierGroups(products[0]).length'),1);
run("stoppedProductIds={hall:true}");
assert.equal(run('productIsAvailable(products[0])'),false);
assert.equal(run('selectedServiceModifier(products[0])'),null);
run("orderMode='takeaway'");
assert.equal(run('productIsAvailable(products[0])'),true);
run('stoppedProductIds={cheese:true}');
assert.equal(run('productIsAvailable(products[0])'),false);
assert.ok(html.includes('Kaspi QR')&&html.includes('Kaspi ·'));
assert.ok(!html.includes('ForteBank'));
run('availabilityReady=true;locationAvailability={acceptsOrders:true,acceptsDelivery:false};availabilityCheckedAt=Date.now()');
assert.equal(run('ordersAvailable()'),true);
run('locationAvailability.acceptsOrders=false');
assert.equal(run('ordersAvailable()'),false);
run('locationAvailability.acceptsOrders=true;availabilityCheckedAt=Date.now()-61000');
assert.equal(run('ordersAvailable()'),false);
run('availabilityCheckedAt=Date.now();availabilityReady=false');
assert.equal(run('ordersAvailable()'),false);
run("stoppedProductIds={abc:true}");
assert.equal(run("isStoppedProductId('ABC')"),true);
console.log('Frontend checks passed: service selection/prices, stop lists, hidden modifiers, Kaspi choices.');

// Exercise the welcome -> language -> service mode flow with the real paint functions.
function node(){
  const classes=new Set();
  return {innerHTML:'',textContent:'',attributes:{},style:{},focus(){this.focused=true},
    querySelector(){return null},querySelectorAll(){return []},setAttribute(key,value){this.attributes[key]=value},
    classList:{add(key){classes.add(key)},remove(key){classes.delete(key)},toggle(key,on){on?classes.add(key):classes.delete(key)},contains(key){return classes.has(key)}}};
}
for(const id of [...html.matchAll(/\bid="([^"]+)"/g)].map(x=>x[1]))nodes[id]=node();
c.document.documentElement={lang:''};
c.document.querySelector=()=>null;
c.document.querySelectorAll=()=>[];
run("products=[];cart=[];categories=[];orderMode=null;languageSelected=false;paint()");
assert.equal(nodes.app.classList.contains('languagePending'),true);
run("chooseOrderMode('dinein')");
assert.equal(run('orderMode'),null,'Service mode cannot bypass the welcome screen');
for(const [language,htmlLang,title] of [['en','en','Where will you eat?'],['kz','kk','Қай жерде жейсіз?'],['ru','ru','Где будете есть?']]){
  run(`startOrdering('${language}')`);
  assert.equal(nodes.app.classList.contains('languagePending'),false);
  assert.equal(nodes.app.classList.contains('modePending'),true);
  assert.equal(c.document.documentElement.lang,htmlLang);
  assert.equal(nodes.orderModeTitle.textContent,title);
  assert.equal(nodes.orderModeTitle.focused,true);
  run("chooseOrderMode('takeaway')");
  assert.equal(nodes.app.classList.contains('modeChosen'),true);
  run('returnToWelcome()');
  assert.equal(nodes.app.classList.contains('languagePending'),true);
  assert.equal(run('orderMode'),null);
}
run("menuLoadError={code:'OFFLINE'};startOrdering('en')");
assert.match(nodes.setup.innerHTML,/Could not load the menu/);
run('availabilityReady=true;availabilityCheckedAt=Date.now();locationAvailability.pickupMinutes=20;paint()');
assert.equal(nodes.availabilityNotice.textContent,'Estimated wait: 20 min');
assert.equal(run("categoryName({name:'Напитки'})"),'Drinks');
assert.equal(run('pointLabel()'),'Arai');
c.window.location.pathname='/respublica';
assert.equal(run('kioskContextFromPath().point'),'RESPUBLIKA');
// Use the real completion handler with synthetic payment/provider responses.
// This verifies UI state reset, not acceptance by Kaspi or iiko.
for(const id of ['kaspiStateTitle','kaspiStateMessage','kaspiStateDetail','kaspiStateActions','finishPaidOrder'])nodes[id]=node();
c.fetch=async()=>({ok:true,json:async()=>({success:true,orderNumber:'offline-42'})});
run('availabilityReady=true;availabilityCheckedAt=Date.now()');
run("cart=[{productId:'p',itemId:'p',q:1,unitPrice:100}];customerPhone='7771234567'");
run("submitPaidOrder('offline-payment',100,'QrTransactionSuccess')").then(async()=>{
  assert.equal(typeof nodes.finishPaidOrder.onclick,'function');
  nodes.finishPaidOrder.onclick();
  assert.equal(run('cart.length'),0);
  assert.equal(run('customerPhone'),'');
  assert.equal(nodes.app.classList.contains('languagePending'),true);
  console.log('Welcome checks passed: three languages, mode gating, offline text, point routing, next guest reset.');
  // Regression: ISSUE-003 — a fast double tap started two Smart POS requests.
  // Found by /qa on 2026-10-14; only mocked providers are used here.
  let paymentStarts=0;
  c.fetch=async url=>{
    if(url==='/kiosk-payment-readiness')return {ok:true,json:async()=>({success:true,ready:true})};
    if(url.endsWith('/api/payment/start')){paymentStarts++;return {ok:true,json:async()=>({ok:true,processId:'offline-process'})};}
    return {ok:true,json:async()=>({ok:true,status:'failure',subStatus:'ProcessCancelled'})};
  };
  run("cart=[{productId:'p',itemId:'p',q:1,unitPrice:100}];availabilityReady=true;availabilityCheckedAt=Date.now()");
  await Promise.all([run('startKaspiPayment()'),run('startKaspiPayment()')]);
  assert.equal(paymentStarts,1,'Double taps must not start duplicate payment requests');
  await run('startKaspiPayment()');
  assert.equal(paymentStarts,2,'A later explicit retry after cancellation remains available');
  console.log('Payment start checks passed: duplicate taps blocked; explicit retry remains available.');
}).catch(error=>{console.error(error);process.exitCode=1});
