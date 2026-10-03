// Offline logic tests. No requests to a bank, CRM or iiko.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../static/kiosk-preview.html'),'utf8');
const code=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const nodes={modalRoot:{innerHTML:''}};
const c=vm.createContext({console,window:{location:{pathname:'/Aray'}},sessionStorage:{setItem(){}},document:{getElementById:id=>nodes[id]||null},clearTimeout(){}});
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
  return {innerHTML:'',textContent:'',attributes:{},focus(){this.focused=true},
    querySelectorAll(){return []},setAttribute(key,value){this.attributes[key]=value},
    classList:{toggle(key,on){on?classes.add(key):classes.delete(key)},contains(key){return classes.has(key)}}};
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
run('availabilityReady=true;availabilityCheckedAt=Date.now();showSuccess()');
// Completion clears the session cart and returns to language selection for the next guest.
run("cart=[{productId:'p',q:1,unitPrice:100}];customerPhone='7771234567'");
nodes.finish.onclick();
assert.equal(run('cart.length'),0);
assert.equal(run('customerPhone'),'');
assert.equal(nodes.app.classList.contains('languagePending'),true);
console.log('Welcome checks passed: three languages, mode gating, offline text, point routing, next guest reset.');
