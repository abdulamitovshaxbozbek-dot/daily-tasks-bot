const fs=require('fs'), vm=require('vm'), assert=require('assert'), path=require('path');
const html=fs.readFileSync(path.join(__dirname,'../api/miniapp/index.html'),'utf8');
let scripts=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
let source=scripts.at(-1);source=source.replace(/\}\)\(\);\s*$/,`globalThis.testUI={renderModel:(data,tab,arch)=>{model=data;section=tab;archived=arch;render();return panel.innerHTML},openForm};})();`);
const els={};function el(name){return els[name]||(els[name]={innerHTML:'',hidden:false,open:false,listeners:{},addEventListener(type,handler){this.listeners[type]=handler},setAttribute(){},querySelector(x){return el(x)},querySelectorAll(){return []},appendChild(){},insertBefore(){},showModal(){this.open=true}})}
const context={document:{hidden:true,querySelector:el,getElementById:el,createElement:el,addEventListener(){}},window:{},location:{search:''},URLSearchParams,crypto:{randomUUID:()=> 'test-request-123456'},setInterval(){},console};vm.createContext(context);vm.runInContext(source,context);
const model={date:'2026-10-03',now:'10:00',habits:[{id:1,title:'Kitob',kind:'habit',active:true,reminder_time:'08:00',streak:{current:2,best:3}},{id:2,title:'Zikr',kind:'zikr',active:false,reminder_time:'09:00',streak:{current:0,best:1}}],entries:[],stats:{daily:{habit:{done:0,total:1}},weekly:{habit:{done:0,total:1}},monthly:{habit:{done:0,total:1}}},calendar:[],regions:{},settings:null,times:{},qaza:[],qaza_completed:0};
let active=context.testUI.renderModel(model,'habits',false);
assert(active.includes('data-action="archive"'));assert(active.includes('data-action="delete"'));assert(!active.includes('data-action="activate"'));assert(active.includes('0 / 1 bajarildi'));
let archived=context.testUI.renderModel(model,'habits',true);
assert(archived.includes('data-action="activate"'));assert(archived.includes('Arxivlangan · eslatmalar o‘chirilgan'));assert(!archived.includes('data-action="archive"'));
context.testUI.openForm('delete',2);assert(els.journeyForm.innerHTML.includes('Tarix va natijalar ham o‘chiriladi'));assert(els.journeyForm.innerHTML.includes('Zikr'));
context.testUI.openForm('activate',2);assert(els.journeyForm.innerHTML.includes('Eski kunlar uchun eslatmalar yuborilmaydi'));
model.entries=[{id:3,ref:'1',kind:'habit',item_kind:'habit',status:'pending',available:true,revision:0}];
let actionable=context.testUI.renderModel(model,'habits',false);assert(actionable.includes('data-status="done"'));assert(actionable.includes('class="j-menu"'));assert(actionable.includes('Natijalarimni ko‘rish'));
model.entries[0]={...model.entries[0],status:'done',can_undo:true,revision:1};let done=context.testUI.renderModel(model,'habits',false);assert(done.includes('data-status="undo"'));assert(!done.includes('data-status="done"'));
context.testUI.openForm('add');assert(els.journeyForm.innerHTML.includes('Takrorlanish'));assert.equal((els.journeyForm.innerHTML.match(/name="weekdays"/g)||[]).length,7);
context.testUI.openForm('later',{id:3,revision:0});assert.equal((els.journeyForm.innerHTML.match(/data-snooze=/g)||[]).length,3);
console.log('Mini App: active/archive views and deletion/activation confirmations passed');

(async()=>{
  model.entries[0]={id:3,ref:'1',kind:'habit',item_kind:'habit',status:'pending',available:true,revision:0};
  context.testUI.renderModel(model,'habits',false);
  let writes=0;
  context.fetch=async(url,options)=>{
    if(options.method==='PUT') { writes++;model.entries[0]={...model.entries[0],status:'done',can_undo:true,revision:1};return {ok:true,json:async()=>({ok:true,message:'Bajarildi'})}; }
    return {ok:true,json:async()=>model};
  };
  const b={dataset:{action:'mark',id:'3',status:'done',revision:'0'}};
  const event={target:{closest:()=>b}};
  await Promise.all([els.section.listeners.click(event),els.section.listeners.click(event)]);
  assert.equal(writes,1);assert(els.section.innerHTML.includes('data-status="undo"'));assert(els.section.innerHTML.includes('Bajarildi'));
  console.log('Mini App: rapid duplicate taps make one request, result and undo render correctly');
})().catch(error=>{console.error(error);process.exitCode=1});
