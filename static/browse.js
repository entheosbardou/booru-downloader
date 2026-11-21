// Saved-result navigation owns its in-flight requests independently of Activity.
let postsController=null,libraryLoaded=false;
function browseRevision(){const found=activeSearch();return found?`${active}:${found.count}:${JSON.stringify(found.children?state.searches.filter(s=>found.children.includes(s.id)).map(s=>s.last_crawl):found.last_crawl)}:${state.settings.default_path}:${$('filter').value?state.tag_index?.updated||0:0}`: ''}
let refreshPending=null;
function refresh({fresh=false}={}){if(refreshPending)return fresh?refreshPending.then(()=>refresh()):refreshPending;refreshPending=refreshLibrary().finally(()=>{refreshPending=null});return refreshPending}
async function refreshLibrary(){try{const library=await api('/library');state={...state,...library,settings:{...state.settings,...library.settings}};libraryLoaded=true;const ti=state.tag_index;$('tag-index-status').textContent=ti&&!ti.ready?`Preparing saved-tag search: ${(ti.completed||0).toLocaleString()} / ${(ti.total||0).toLocaleString()} records indexed. Filtered results will grow as indexing continues.${ti.error?' Indexing paused; retrying automatically.':''}`:'';$('source-name').textContent='Booru collection desk';document.title='Booru · Collection desk';renderLibrary();renderSavedCrawlSites();$('storage-label').textContent=state.settings.default_path||'Not configured · downloads off';if(document.activeElement!==$('default-path')&&$('settings').classList.contains('hidden')){$('default-path').value=state.settings.default_path||'';$('delay').value=state.settings.delay>0?Number((60/state.settings.delay).toPrecision(8)):0}if(active!==null&&!hasSearch(active)){active=null;current=[];selected.clear();renderKey='';$('results-title').textContent='Search results';$('count').textContent='';$('pagination').classList.add('hidden');for(const fmt of ['json','csv'])$('export-'+fmt).classList.add('hidden');renderPosts()}if(active===null&&state.collections?.length){await choose('g-'+state.collections[0].id);return}const found=activeSearch();$('count').textContent=found?`${found.count.toLocaleString()} saved`:'';const key=browseRevision();if(key!==renderKey&&!pageLoading){await loadPosts()}setBusyControls()}catch(e){if(!libraryLoaded){$('search-list').replaceChildren(el('p','Could not load saved searches.','muted'));const retry=el('button','Retry loading');retry.onclick=()=>refresh();$('search-list').append(retry);$('storage-label').textContent='Settings unavailable'}toast(e.message,true)}}
async function choose(id){active=id;renderLibrary();page=1;selected.clear();renderKey='';current=[];renderPosts();$('pagination').classList.add('hidden');$('browse-total').textContent='';$('page-label').textContent='';$('page-number').value=1;$('filter').value='';$('results-title').textContent=(typeof id==='string'?state.collections.find(g=>'g-'+g.id===id):state.searches.find(s=>s.id===id))?.display_name||(typeof id==='string'?state.collections.find(g=>'g-'+g.id===id):state.searches.find(s=>s.id===id))?.tags||'Search results';for(const fmt of ['json','csv']){const link=$('export-'+fmt);link.href=`/api${activePath(id)}/export.${fmt}`;link.classList.remove('hidden')}await loadPosts()}
async function loadPosts({scrollTop=false}={}){
 if(active===null)return;
 const requestId=++postsRequest,sid=active,size=perPage,term=$('filter').value,sort=resultSort;
 postsController?.abort();
 const controller=postsController=new AbortController();
 const revision=browseRevision();
 let expectedPage=page,offset=0,failed=false;
 const loaded=new Map();pageLoading=true;$('page-loading').textContent='Loading results…';setBusyControls();
 try{
  do{
   const data=await api(`${activePath(sid)}/posts?page=${expectedPage}&per_page=${size}&filter=${encodeURIComponent(term)}&sort=${encodeURIComponent(sort)}&chunk_size=${term?size:Math.min(size,48)}&chunk_offset=${offset}`,undefined,{signal:controller.signal});
   if(requestId!==postsRequest||sid!==active||expectedPage!==page||size!==perPage||sort!==resultSort||term!==$('filter').value||!hasSearch(sid))return;
   renderKey=revision;page=data.page;expectedPage=page;totalPages=data.total_pages;
   for(const post of data.posts)loaded.set(post.record_id,post);
   current=[...loaded.values()];for(const post of current)if(post.queued||post.downloaded)selected.delete(post.record_id);
   renderPosts();$('pagination').classList.toggle('hidden',!data.total);$('page-label').textContent=`Page ${page} of ${totalPages.toLocaleString()}`;
   $('page-number').value=page;$('page-number').max=totalPages;$('browse-total').textContent=`of ${totalPages.toLocaleString()} pages · ${data.total.toLocaleString()} matching results`;
   $('previous').disabled=page<=1;$('next').disabled=page>=totalPages;
   $('page-loading').textContent=data.more_on_page?`Loading results… ${current.length} / ${data.page_length}`:'';
   setBusyControls();if(scrollTop&&offset===0)window.scrollTo({top:0,behavior:'smooth'});
   offset+=data.posts.length;if(!data.more_on_page||!data.posts.length)break;
  }while(offset<size);
 }catch(e){if(controller.signal.aborted)return;failed=true;if(requestId===postsRequest)$('page-loading').textContent='Loading interrupted · use Go to retry.';throw e}
 finally{if(requestId===postsRequest){pageLoading=false;if(!failed)$('page-loading').textContent='';setBusyControls()}}
}


async function refreshVisiblePostStatus(){if(active===null||!current.length)return;const sid=active,requestId=postsRequest;const data=await api(`${activePath(sid)}/posts/status`,{ids:current.map(p=>p.record_id)});if(sid!==active||requestId!==postsRequest)return;const statuses=new Map(data.posts.map(p=>[p.record_id,p]));for(const post of current){const status=statuses.get(post.record_id);if(status){post.downloaded=status.downloaded;post.queued=status.queued;if(post.queued||post.downloaded)selected.delete(post.record_id)}}renderPosts();setBusyControls()}
