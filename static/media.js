// Preserve loaded images when only download/selection state changes.
function visiblePostSignature(posts,previews){
  return JSON.stringify([previews,posts.map(p=>[p.record_id,p.md5,p.checksum,p.width,p.height,p.rating,p.tags,p.metadata_note,p.site_name,p.post_url,!!p.preview_url,!!p.downloaded])]);
}

function canQueuePost(post){
  return !post.queued&&!post.downloaded&&!post.status_pending;
}

function updatePostSelection(check,post,selected){
  check.checked=selected.has(post.record_id);
  if(check.checked)selected.set(post.record_id,canQueuePost(post));
}

function refreshPostCardStates(grid,posts,selected,downloadPath){
  const cards=new Map([...grid.children].map(card=>[Number(card.dataset.recordId),card]));
  for(const p of posts){
    const card=cards.get(p.record_id);if(!card)continue;
    const check=card.querySelector('input');
    updatePostSelection(check,p,selected);
    const saved=card.querySelector('.badge.done');
    if(p.downloaded&&!saved){const badge=document.createElement('span');badge.className='badge done';badge.textContent='Saved';card.querySelector('.card-top').append(badge)}
    if(!p.downloaded&&saved)saved.remove();
    const button=card.querySelector('.entry-download');
    button.textContent=p.queued?'Queued':p.downloaded?'Open saved file':'Download';
    button.disabled=p.queued||p.status_pending||(!p.downloaded&&!downloadPath);
    button.onclick=()=>p.downloaded?window.open(`/api/posts/${p.record_id}/file`,'_blank','noopener'):queueDownloads([p.record_id]);
  }
}

// Wait outside the two network slots so one site's cooldown cannot stall others.
const previewPending=[];
const previewTasks=new Set();
let previewActive=0;
function enqueuePreviewFallback(img,recordId,failed){
  const task={img,recordId,failed,attempts:0,deadline:Date.now()+120000,controller:null,timer:null};
  previewTasks.add(task);previewPending.push(task);drainPreviewQueue();
}
function finishPreview(task,failed=false){
  clearTimeout(task.timer);previewTasks.delete(task);
  if(failed&&task.img.isConnected)task.failed();
}
function cancelDetachedPreviews(){
  for(const task of previewTasks){
    if(!task.img.isConnected){task.controller?.abort();finishPreview(task)}
  }
}
function drainPreviewQueue(){
  cancelDetachedPreviews();
  while(previewActive<2&&previewPending.length){
    const task=previewPending.shift();
    if(!previewTasks.has(task))continue;
    if(Date.now()>=task.deadline){finishPreview(task,true);continue}
    previewActive++;
    fetchQueuedPreview(task).finally(()=>{previewActive--;drainPreviewQueue()});
  }
}
function previewRetryDelay(value,now=Date.now()){
  if(value===null)return 1000;
  const seconds=Number(value);
  return Number.isFinite(seconds)?Math.max(1000,seconds*1000):Math.max(1000,(Date.parse(value)||now)-now);
}
async function fetchQueuedPreview(task){
  const {img,recordId}=task;
  const controller=task.controller=new AbortController();
  const timeout=setTimeout(()=>controller.abort(),Math.min(45000,task.deadline-Date.now()));
  try{
    task.attempts++;
    const response=await fetch('/api/posts/'+recordId+'/preview',{signal:controller.signal});
    if(!img.isConnected){finishPreview(task);return}
    if(response.status===503&&response.headers.get('X-Preview-Busy')==='1'){
      const delay=previewRetryDelay(response.headers.get('Retry-After'));
      // Release the response and slot before the cooldown, and stop after a bounded retry budget.
      await response.body?.cancel();
      if(task.attempts>=8||Date.now()+delay>=task.deadline){finishPreview(task,true);return}
      task.timer=setTimeout(()=>{
        if(!previewTasks.has(task))return;
        previewPending.push(task);drainPreviewQueue();
      },delay);
      return;
    }
    if(!response.ok)throw Error('Preview request failed');
    const blob=await response.blob();
    if(!img.isConnected){finishPreview(task);return}
    const url=URL.createObjectURL(blob);
    img.addEventListener('load',()=>URL.revokeObjectURL(url),{once:true});
    img.addEventListener('error',()=>URL.revokeObjectURL(url),{once:true});
    img.src=url;finishPreview(task);
  }catch(error){finishPreview(task,true)}
  finally{clearTimeout(timeout);task.controller=null}
}
// Cancel stale network reads immediately when a new page replaces the cards.
new MutationObserver(cancelDetachedPreviews).observe(document.getElementById('grid'),{childList:true,subtree:true});
