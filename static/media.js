// Preserve loaded images when only download/selection state changes.
function visiblePostSignature(posts,previews){
  return JSON.stringify([previews,posts.map(p=>[p.record_id,p.md5,p.checksum,p.width,p.height,p.rating,p.tags,p.metadata_note,p.site_name,p.post_url,!!p.preview_url])]);
}

function refreshPostCardStates(grid,posts,selected,downloadPath){
  const cards=new Map([...grid.children].map(card=>[Number(card.dataset.recordId),card]));
  for(const p of posts){
    const card=cards.get(p.record_id);if(!card)continue;
    const check=card.querySelector('input');
    check.disabled=p.queued||p.downloaded;if(check.disabled)selected.delete(p.record_id);check.checked=!check.disabled&&selected.has(p.record_id);
    const saved=card.querySelector('.badge.done');
    if(p.downloaded&&!saved){const badge=document.createElement('span');badge.className='badge done';badge.textContent='Saved';card.querySelector('.card-top').append(badge)}
    if(!p.downloaded&&saved)saved.remove();
    const button=card.querySelector('.entry-download');
    button.textContent=p.queued?'Queued':p.downloaded?'Open saved file':'Download';
    button.disabled=p.queued||(!p.downloaded&&!downloadPath);
    button.onclick=()=>p.downloaded?window.open(`/api/posts/${p.record_id}/file`,'_blank','noopener'):queueDownloads([p.record_id]);
  }
}

const previewPending=[];
let previewActive=0;
function enqueuePreviewFallback(img,recordId,failed){
  previewPending.push({img,recordId,failed});
  drainPreviewQueue();
}
function drainPreviewQueue(){
  while(previewActive<2&&previewPending.length){
    const task=previewPending.shift();
    if(!task.img.isConnected)continue;
    previewActive++;
    fetchQueuedPreview(task).finally(()=>{previewActive--;drainPreviewQueue()});
  }
}
async function fetchQueuedPreview({img,recordId,failed}){
  try{
    while(img.isConnected){
      const controller=new AbortController();
      const timeout=setTimeout(()=>controller.abort(),60000);
      let response;
      try{response=await fetch('/api/posts/'+recordId+'/preview',{signal:controller.signal})}
      finally{clearTimeout(timeout)}
      if(response.status===503&&response.headers.get('X-Preview-Busy')==='1'){
        await new Promise(resolve=>setTimeout(resolve,1000));continue;
      }
      if(!response.ok)throw Error('Preview request failed');
      const blob=await response.blob();
      if(!img.isConnected)return;
      const url=URL.createObjectURL(blob);
      img.addEventListener('load',()=>URL.revokeObjectURL(url),{once:true});
      img.addEventListener('error',()=>URL.revokeObjectURL(url),{once:true});
      img.src=url;return;
    }
  }catch(error){if(img.isConnected)failed()}
}
