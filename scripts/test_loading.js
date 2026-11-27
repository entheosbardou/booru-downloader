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
function browseRuntime(handlers){
 const nodes=new Map();const node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',disabled:false,max:0,classList:{toggle(){},remove(){}}});return nodes.get(id)};
 const rt=runtime({$:node,active:7,postsRequest:0,perPage:48,resultSort:'post_newest',page:1,selected:new Set(),
  state:{settings:{}},current:[],renderKey:'',activeSearch:()=>({count:2}),activePath:id=>'/searches/'+id,
  hasSearch:()=>true,setBusyControls(){},renderPosts(){},toast(){},api:handlers});
 rt.run(source('browse.js'));rt.nodes=nodes;return rt;
}
async function testStalePageCancellation(){
 const pending=[];
 const rt=browseRuntime((url,body,options)=>{
  if(url.includes('/posts/count'))return Promise.resolve({total:1,total_pages:1,per_page:48});
  if(url.includes('/posts/status'))return Promise.resolve({posts:[]});
  return new Promise(resolve=>pending.push({resolve,signal:options.signal}))});
 const first=rt.run('loadPosts()');rt.context.active=8;const second=rt.run('loadPosts()');
 assert.equal(pending[0].signal.aborted,true);
 const page=id=>({page:1,posts:[{record_id:id}],more_on_page:false,has_next:false});
 pending[1].resolve(page(8));await second;pending[0].resolve(page(7));await first;
 assert.equal(rt.context.current[0].record_id,8,'late results cannot overwrite the chosen search');
}
async function testSectionsLoadIndependently(){
 const calls=[];
 // The total never answers, yet the grid still renders and the load completes.
 let rt=browseRuntime((url,body,options)=>{calls.push(url);
  if(url.includes('/posts/count'))return new Promise(()=>{});
  if(url.includes('/posts/status'))return Promise.resolve({posts:[]});
  return Promise.resolve({page:1,posts:[{record_id:5}],more_on_page:false,has_next:true})});
 await rt.run('loadPosts()');
 assert.equal(rt.context.current[0].record_id,5,'results must not wait for the total');
 assert.ok(calls.some(u=>u.includes('/posts/page'))&&calls.some(u=>u.includes('/posts/count')),'page and total use separate requests');
 assert.equal(rt.nodes.get('browse-total').textContent,'Counting matching results…');
 assert.equal(rt.nodes.get('next').disabled,false,'next is allowed from the page response alone');
 // A failed total is reported in its own place and leaves the grid usable.
 rt=browseRuntime(url=>{
  if(url.includes('/posts/count'))return Promise.reject(Error('slow'));
  if(url.includes('/posts/status'))return Promise.resolve({posts:[]});
  return Promise.resolve({page:1,posts:[{record_id:6}],more_on_page:false,has_next:false})});
 await rt.run('loadPosts()');await flush();
 assert.equal(rt.context.current[0].record_id,6);
 assert.match(rt.nodes.get('browse-total').textContent,/Total unavailable/);
 // A failing status request must not break or block the grid either.
 rt=browseRuntime(url=>{
  if(url.includes('/posts/count'))return Promise.resolve({total:1,total_pages:1,per_page:48});
  if(url.includes('/posts/status'))return Promise.reject(Error('status down'));
  return Promise.resolve({page:1,posts:[{record_id:7,status_pending:true}],more_on_page:false,has_next:false})});
 await rt.run('loadPosts()');await flush();
 assert.equal(rt.context.current[0].record_id,7);
 assert.match(rt.nodes.get('browse-total').textContent,/1 matching results/);
}
async function testTagCountOnDemand(){
 const calls=[];
 const rt=browseRuntime(url=>{
  calls.push(url);
  if(url.includes('/posts/count'))return Promise.resolve({total:49,total_pages:2,per_page:48});
  if(url.includes('/posts/status'))return Promise.resolve({posts:[]});
  return Promise.resolve({posts:[{record_id:7}],more_on_page:false,has_next:true});
 });
 rt.run("$('filter').value='blue_hair'");
 await rt.run('loadPosts()');
 const button=rt.nodes.get('count-matches');
 assert.equal(button.hidden,false);
 assert.equal(calls.filter(url=>url.includes('/posts/count')).length,0);
 assert.equal(rt.nodes.get('next').disabled,false);
 await button.onclick();
 assert.equal(button.hidden,true);
 assert.match(rt.nodes.get('browse-total').textContent,/49 matching results/);
 rt.context.page=2;
 await rt.run('loadPosts()');
 await flush();
 assert.equal(calls.filter(url=>url.includes('/posts/count')).length,1);
 assert.equal(rt.nodes.get('next').disabled,true);
}
(async()=>{
 await testCooldown();await testDetachedImage();await testRequestBodyTimeout();await testStalePageCancellation();await testSectionsLoadIndependently();
 await testTagCountOnDemand();
 console.log('PASS: preview cooldown isolation, detached cancellation, body timeout, stale page cancellation, independent loading, and on-demand tag counts');
})().catch(error=>{console.error(error);process.exitCode=1});
