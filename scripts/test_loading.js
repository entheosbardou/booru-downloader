// Deterministic browser behavior checks without a server or external packages.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=name=>fs.readFileSync(path.join(__dirname,'..','static',name),'utf8');
const flush=async()=>{for(let n=0;n<12;n++)await Promise.resolve()};
function runtime(extra={}){
 let now=0,id=0;const timers=new Map();
 const context=vm.createContext({AbortController,console,Date:class extends Date{static now(){return now}},
   setTimeout:(fn,delay)=>{timers.set(++id,{fn,due:now+delay});return id},clearTimeout:id=>timers.delete(id),
   URL:{createObjectURL:()=> 'blob:fixture',revokeObjectURL:()=>{}},
   MutationObserver:class{observe(){}},document:{getElementById:()=>({})},...extra});
 return {context,run:code=>vm.runInContext(code,context),async tick(ms){now+=ms;
   for(const [id,timer] of [...timers])if(timer.due<=now){timers.delete(id);timer.fn()}
   await flush();
 }};
}
const image=()=>({isConnected:true,src:'',addEventListener(){}});
const busy=()=>({ok:false,status:503,headers:{get:name=>name==='X-Preview-Busy'?'1':'60'},body:{cancel:async()=>{}}});
const success=()=>({ok:true,status:200,blob:async()=>({})});
async function testCooldown(){
 const calls=[];let failures=0;
 const rt=runtime({fetch:async url=>{calls.push(url);return url.includes('/1/')?busy():success()}});
 rt.run(source('media.js'));Object.assign(rt.context,{first:image(),second:image(),third:image(),failed:()=>failures++});
 rt.run('enqueuePreviewFallback(first,1,failed);enqueuePreviewFallback(second,2,failed);enqueuePreviewFallback(third,3,failed)');
 await flush();assert.equal(calls.length,3,'a cooldown must free a network slot');
 assert.equal(rt.context.second.src,'blob:fixture');assert.equal(rt.context.third.src,'blob:fixture');
 await rt.tick(59000);assert.equal(calls.length,3,'honor the full Retry-After interval');
 await rt.tick(1000);assert.equal(calls.length,4);assert.equal(failures,1,'end retries at the deadline');
}
async function testDetachedImage(){
 let signal,failures=0;
 const rt=runtime({fetch:(url,options)=>new Promise((resolve,reject)=>{signal=options.signal;signal.addEventListener('abort',()=>reject(Object.assign(Error(),{name:'AbortError'})))})});
 rt.run(source('media.js'));Object.assign(rt.context,{img:image(),failed:()=>failures++});
 rt.run('enqueuePreviewFallback(img,1,failed)');await flush();
 rt.context.img.isConnected=false;rt.run('cancelDetachedPreviews()');await flush();
 assert.equal(signal.aborted,true);assert.equal(failures,0,'detached cards must not report a failure');
 assert.equal(rt.run('previewActive'),0);
}
async function testRequestBodyTimeout(){
 const rt=runtime({fetch:async(url,options)=>({ok:true,json:()=>new Promise((resolve,reject)=>{
   options.signal.addEventListener('abort',()=>reject(Object.assign(Error(),{name:'AbortError'})))
 })})});
 rt.run(source('api.js'));const result=rt.run("api('/library',undefined,{timeout:50})");
 const check=assert.rejects(result,/timed out/);await flush();await rt.tick(50);await check;
}
async function testStalePageCancellation(){
 const nodes=new Map();const node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',classList:{toggle(){}}});return nodes.get(id)};
 const pending=[];
 const rt=runtime({$:node,active:7,postsRequest:0,perPage:48,resultSort:'post_newest',page:1,selected:new Set(),
  state:{settings:{}},current:[],renderKey:'',activeSearch:()=>({count:2}),activePath:id=>'/searches/'+id,
  hasSearch:()=>true,setBusyControls(){},renderPosts(){},
  api:(url,body,options)=>new Promise(resolve=>pending.push({resolve,signal:options.signal}))});
 rt.run(source('browse.js'));
 const first=rt.run('loadPosts()');rt.context.active=8;const second=rt.run('loadPosts()');
 assert.equal(pending[0].signal.aborted,true);
 const page=id=>({page:1,total_pages:1,total:1,posts:[{record_id:id}],more_on_page:false});
 pending[1].resolve(page(8));await second;pending[0].resolve(page(7));await first;
 assert.equal(rt.context.current[0].record_id,8,'late results cannot overwrite the chosen search');
}
(async()=>{
 await testCooldown();await testDetachedImage();await testRequestBodyTimeout();await testStalePageCancellation();
 console.log('PASS: preview cooldown isolation, detached cancellation, body timeout, and stale page cancellation');
})().catch(error=>{console.error(error);process.exitCode=1});
