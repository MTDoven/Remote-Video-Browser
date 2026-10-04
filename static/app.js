/* Shared responsive library and native/MSE HLS playback. No video transcoding. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const player = $('player');
  let route, viewController, viewNumber = 0, visibleIds = new Set(), observer, imageURLs = new Set(), listObserver, viewState;
  const views = new Map();
  let positions = {};
  try {positions = JSON.parse(sessionStorage.getItem('library-positions') || '{}');} catch (_) {}
  history.scrollRestoration = 'manual';
  let playController, playNumber = 0, current, hls, heartbeat, seekTimer, recoveryTimer, searchTimer, previewController;
  let viewOwner = '', previewOwner = '', previewId;
  let seekNumber = 0, lastSeek = 0, recoveries = 0, recovering = false;
  let viewing, viewingSequence = 0, viewedRanges = [], playbackSample;
  const historyQueue = new Map();
  let rootId, initialScan = true, refreshPending = false;
  const wait = (ms, signal) => new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException('Cancelled', 'AbortError'));
    const timer = setTimeout(done, ms);
    function done() { signal?.removeEventListener('abort', abort); resolve(); }
    function abort() { clearTimeout(timer); reject(new DOMException('Cancelled', 'AbortError')); }
    signal?.addEventListener('abort', abort, {once:true});
  });
  function show(id, text, error = false) {
    const node = $(id); node.textContent = text; node.hidden = !text; node.classList.toggle('error', error);
  }
  async function api(path, options = {}) {
    const response = await fetch(path, {cache:'no-store', ...options});
    const data = response.status === 204 ? null : await response.json();
    if (!response.ok) throw new Error(data?.error || `Request failed (${response.status})`);
    return {data, status:response.status};
  }
  const json = data => ({headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const formatBytes = bytes => bytes >= 1024**3 ? `${(bytes/1024**3).toFixed(1)} GiB` : `${(bytes/1024**2).toFixed(1)} MiB`;
  const duration = seconds => { const s = Math.round(seconds); return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`; };
  function link(state) { return '#' + new URLSearchParams(Object.entries(state).filter(([,v])=>v !== undefined && v !== '')).toString(); }
  function navigate(state) { saveView(); const url = link(state); if (location.hash === url) loadView(); else location.hash = url; }
  function parseRoute() {
    const params = new URLSearchParams(location.hash.slice(1));
    const q=params.get('q')||'', mode=params.get('mode');
    return {mode:q?'items':mode==='items'?'items':mode==='recommendations'?'recommendations':'directory', dir:params.get('dir')||rootId,q};
  }
  function el(tag, className, text) { const n=document.createElement(tag); if(className)n.className=className;if(text!==undefined)n.textContent=text;return n; }
  function cancelView() {
    viewController?.abort(); observer?.disconnect(); listObserver?.disconnect();
    if (visibleIds.size) fetch('/api/previews/cancel',{method:'POST',...json({ids:[...visibleIds],owner:viewOwner}),keepalive:true}).catch(()=>{});
    visibleIds.clear(); imageURLs.forEach(URL.revokeObjectURL); imageURLs.clear();
  }
  async function imageBlob(id, kind, signal, version, owner = viewOwner) {
    const path=`/api/videos/${id}/images/${kind}`+(version?`?version=${version}`:'');
    for (;;) {
      const response=await fetch(path,{signal,cache:'no-store',headers:{'X-View-ID':owner}});
      if(response.status===202) { await wait(700,signal); continue; }
      if(!response.ok) { const data=await response.json(); throw new Error(data.error||'Preview is unavailable.'); }
      return URL.createObjectURL(await response.blob());
    }
  }
  function watchCover(img, id, version) {
    img.dataset.id=id; if(version)img.dataset.version=version; observer.observe(img.parentElement);
  }
  function card(entry, isVideo) {
    const node=el('article','card'); node.dataset.entryId=entry.id; if(current?.info.id===entry.id && isVideo)node.classList.add('selected');
    const button=el('button','card-main'); button.type='button'; button.setAttribute('aria-label',isVideo?`Play ${entry.name}`:`Open ${entry.name}`);
    const cover=el('div','cover'); cover.append(el('span','',isVideo?'▶':'▤'));
    const id=isVideo?entry.id:entry.cover;
    if(id) { const img=el('img'); img.alt=''; img.hidden=true; img.decoding='async'; cover.append(img); watchCover(img,id,isVideo?entry.version:null); }
    cover.append(el('span','cover-label',isVideo?'Original':`${entry.video_count} videos`));
    const copy=el('div','card-copy'); copy.append(el('div','card-name',entry.name),el('div','card-path',entry.path));
    button.append(cover,copy); button.addEventListener('click',()=>isVideo?play(entry):navigate({mode:'directory',dir:entry.id}));
    node.append(button);
    const foot=el('div','card-foot'); foot.append(el('span','',isVideo?formatBytes(entry.size):'Open item'));
    if(isVideo) { const preview=el('button','preview-button','Detailed preview'); preview.type='button'; preview.addEventListener('click',()=>openPreview(entry)); foot.append(preview); }
    node.append(foot); return node;
  }
  function viewKey(value) {return JSON.stringify([value.mode,value.mode==='directory'?value.dir:'',value.q]);}
  function saveView() {
    if(!route||!viewState)return;
    viewState.scroll=scrollY;
    const header=document.querySelector('.header').getBoundingClientRect().bottom;
    const anchor=[...$('grid').children].find(node=>node.dataset.entryId&&node.getBoundingClientRect().bottom>header);
    viewState.anchor=anchor?{id:anchor.dataset.entryId,top:anchor.getBoundingClientRect().top}:null;
    positions[viewKey(route)]={scroll:scrollY,pages:viewState.pages,seed:viewState.seed,anchor:viewState.anchor};
    try {sessionStorage.setItem('library-positions',JSON.stringify(positions));} catch (_) {}
  }
  function renderHeading(state) {
    $('title').textContent=state.title;
    $('eyebrow').textContent=route.mode==='directory'?'BROWSE DIRECTORY':route.mode==='items'?'ITEM SEARCH':'DISCOVER VIDEOS';
    $('count').textContent=state.total===undefined?`${state.entries.length.toLocaleString()} videos`:`${state.total.toLocaleString()} entries`;
    $('breadcrumbs').replaceChildren();
    for(const [i,crumb] of (state.crumbs||[]).entries()) {
      if(i)$('breadcrumbs').append(el('span','','/'));
      const a=el('a','',crumb.name);a.href=link({mode:'directory',dir:crumb.id});$('breadcrumbs').append(a);
    }
  }
  async function appendPage(generation=viewNumber) {
    const state=viewState, signal=viewController.signal;
    if(state.loading||state.done||generation!==viewNumber)return;
    state.loading=true;let completed=false;show('load-status','Loading…');
    try {
      let entries;
      if(route.mode==='recommendations') {
        const params=new URLSearchParams({seed:state.seed});if(state.cursor)params.set('cursor',state.cursor);
        const {data}=await api(`/api/recommendations?${params}`,{signal});
        if(generation!==viewNumber)return;
        entries=data.entries.map(e=>[e,true]);state.cursor=data.next;state.done=!data.next;
        state.title=data.unseen?'Recommended unwatched videos':'Recommended revisits';
      } else if(route.mode==='items') {
        const {data}=await api(`/api/items?${new URLSearchParams({q:route.q,page:state.pages+1})}`,{signal});
        if(generation!==viewNumber)return;
        entries=data.entries.map(e=>[e,false]);state.total=data.total;state.done=data.page>=data.pages;
        state.title=route.q?`Results for “${route.q}”`:'All items';
      } else {
        const {data}=await api(`/api/directories/${route.dir}?page=${state.pages+1}`,{signal});
        if(generation!==viewNumber)return;
        entries=[...data.directories.entries.map(e=>[e,false]),...data.videos.entries.map(e=>[e,true])];
        state.total=data.directories.total+data.videos.total;state.done=data.videos.page>=data.videos.pages;
        state.title=data.directory.name;state.crumbs=data.directory.breadcrumbs;
      }
      const seen=new Set(state.entries.map(([e])=>e.id));entries=entries.filter(([e])=>!seen.has(e.id));
      state.entries.push(...entries);state.pages++;
      for(const [entry,isVideo] of entries)$('grid').append(card(entry,isVideo));
      renderHeading(state);
      if(!state.entries.length)$('grid').replaceChildren(el('div','empty','No matching videos or items.'));
      show('load-status',state.done?'End of list':'Scroll to load more');
      saveView();completed=true;
    } catch(error) {if(error.name!=='AbortError'){show('library-message',error.message,true);show('load-status','Retry loading');}}
    finally {
      state.loading=false;
      if(completed&&generation===viewNumber&&!state.restoring&&!state.done&&$('load-status').getBoundingClientRect().top<innerHeight+500)
        setTimeout(()=>appendPage(generation),0);
    }
  }
  async function loadView({refresh=false}={}) {
    saveView();cancelView();const generation=++viewNumber;
    viewController=new AbortController();const signal=viewController.signal;
    viewOwner=`view-${Date.now()}-${Math.random()}`;route=parseRoute();$('search').value=route.q;
    const key=viewKey(route), saved=positions[key]||{};
    if(refresh)for(const cachedKey of views.keys())if(JSON.parse(cachedKey)[0]!=='recommendations')views.delete(cachedKey);
    const cached=views.get(key);
    viewState=cached?{...cached,entries:[...cached.entries]}:{entries:[],pages:0,done:false,cursor:null,seed:saved.seed||`${Date.now()}-${Math.random()}`,scroll:saved.scroll||0,anchor:saved.anchor,title:'Loading…'};
    viewState.loading=false;viewState.restoring=true;views.delete(key);views.set(key,viewState);
    while(views.size>12) {
      const oldest=[...views.keys()].find(cachedKey=>JSON.parse(cachedKey)[0]!=='recommendations');
      views.delete(oldest);
    }
    const restore=viewState.scroll, restoreAnchor=viewState.anchor, restorePages=saved.pages||1;
    $('items-tab').classList.toggle('active',route.mode!=='directory');$('folders-tab').classList.toggle('active',route.mode==='directory');
    $('refresh-recommendations').hidden=route.mode!=='recommendations';
    $('grid').replaceChildren();show('library-message','');
    observer=new IntersectionObserver(entries=>entries.forEach(async entry=>{
      if(signal.aborted)return;
      const img=entry.target.querySelector('img');
      if(!entry.isIntersecting) {
        img.coverController?.abort();img.coverController=null;
        if(img.coverURL){URL.revokeObjectURL(img.coverURL);imageURLs.delete(img.coverURL);img.coverURL=null;img.removeAttribute('src');img.hidden=true;}
        if(visibleIds.delete(img.dataset.id))fetch('/api/previews/cancel',{method:'POST',...json({ids:[img.dataset.id],owner:viewOwner})}).catch(()=>{});
        return;
      }
      if(img.coverController||img.coverURL)return;
      const controller=new AbortController(), abort=()=>controller.abort();img.coverController=controller;
      signal.addEventListener('abort',abort,{once:true});visibleIds.add(img.dataset.id);
      try {
        const url=await imageBlob(img.dataset.id,'preview',controller.signal,img.dataset.version);
        if(signal.aborted||controller.signal.aborted){URL.revokeObjectURL(url);return;}
        imageURLs.add(url);img.coverURL=url;img.src=url;img.hidden=false;
      } catch(error){if(error.name!=='AbortError')img.parentElement.title=error.message;}
      finally {signal.removeEventListener('abort',abort);if(img.coverController===controller)img.coverController=null;}
    }),{rootMargin:'150px'});
    if(viewState.entries.length) {
      for(const [entry,isVideo] of viewState.entries)$('grid').append(card(entry,isVideo));renderHeading(viewState);
    } else {
      for(let i=0;i<restorePages&&!viewState.done;i++){await appendPage(generation);if(generation!==viewNumber||signal.aborted)return;}
    }
    if(generation!==viewNumber)return;
    renderHeading(viewState);
    if(viewState.done&&!viewState.entries.length)$('grid').replaceChildren(el('div','empty','No matching videos or items.'));
    await new Promise(requestAnimationFrame);
    const anchor=restoreAnchor?[...$('grid').children].find(node=>node.dataset.entryId===restoreAnchor.id):null;
    window.scrollTo(0,anchor?anchor.getBoundingClientRect().top+scrollY-restoreAnchor.top:restore);saveView();
    show('load-status',viewState.done?'End of list':'Scroll to load more');
    viewState.restoring=false;
    listObserver=new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting))appendPage(generation);},{rootMargin:'500px'});
    listObserver.observe($('load-status'));
  }
  async function status() {
    if(historyQueue.size)flushViewing();
    try {
      const {data}=await api('/api/status'); rootId=data.root;
      $('cache-status').textContent=`Cache ${formatBytes(data.cache.bytes)} / ${formatBytes(data.cache.limit)}`;
      if(data.cache.pressure)$('cache-status').textContent+=' · Insufficient space; new cache writes are paused';
      if(data.scan.state==='scanning') { $('library-status').textContent=`Refreshing the index · ${data.scan.videos.toLocaleString()} videos`; $('refresh').disabled=true; }
      else { $('refresh').disabled=false; $('library-status').textContent=data.scan.error||`${data.items.toLocaleString()} items · ${data.videos.toLocaleString()} videos · Read-only source`;
        if(data.scan.state==='ready'&&(initialScan||refreshPending)) {initialScan=false;refreshPending=false;loadView({refresh:true});} }
    } catch(error) { $('library-status').textContent=error.message; }
  }
  async function openPreview(entry) {
    previewController?.abort(); previewController=new AbortController(); const signal=previewController.signal;
    previewOwner=`preview-${Date.now()}-${Math.random()}`;previewId=entry.id; $('preview-title').textContent=entry.name; $('preview-image').hidden=true; $('preview-status').textContent='Preparing the preview…'; $('preview').showModal();
    try {
      const url=await imageBlob(entry.id,'preview',signal,entry.version,previewOwner);$('preview-image').onload=()=>URL.revokeObjectURL(url);$('preview-image').src=url;$('preview-image').hidden=false;$('preview-status').textContent='Frames at 20%, 40%, 60%, and 80%';
    } catch(error) {if(error.name!=='AbortError')$('preview-status').textContent=error.message;}
  }
  function stopPlayback() {
    samplePlayback();flushViewing(true);viewing=null;playbackSample=null;
    playNumber++; seekNumber++; playController?.abort(); clearInterval(heartbeat);clearTimeout(seekTimer);clearTimeout(recoveryTimer);
    if(current)fetch(`/api/sessions/${current.id}`,{method:'DELETE',keepalive:true}).catch(()=>{});
    hls?.destroy();hls=null;current=null;player.pause();player.removeAttribute('src');player.load();
    player.hidden=true;$('player-empty').hidden=false;$('close-player').hidden=true;$('fullscreen').hidden=true;$('play-button').hidden=true;
    $('player-title').textContent='No video selected';$('player-meta').textContent='Open an item to browse its videos.';show('player-status','');
    document.querySelectorAll('.card.selected').forEach(c=>c.classList.remove('selected'));
  }
  function segmentAt(time) {
    if(!current)return 0; const times=current.boundaries;let lo=0,hi=times.length-2;
    while(lo<hi){const mid=Math.ceil((lo+hi)/2);if(times[mid]<=time)lo=mid;else hi=mid-1;}return lo;
  }
  async function touch(index, sequence = seekNumber) {
    if(!current)return;const generation=playNumber;
    try {await api(`/api/sessions/${current.id}`,{method:'PATCH',...json({index,sequence})});}
    catch(error){if(generation===playNumber&&sequence===seekNumber)show('player-status',error.message,true);}
  }
  async function play(entry) {
    stopPlayback(); const generation=playNumber;playController=new AbortController(); const signal=playController.signal;
    $('player-title').textContent=entry.name;show('player-status','Reading video metadata…');$('close-player').hidden=false;
    recoveries=0;recovering=false;
    try {
      const opened=await api(`/api/videos/${entry.id}/opens`,{method:'POST',signal});
      if(generation!==playNumber)return;viewing=opened.data.id;viewingSequence=0;viewedRanges=[];
      let info;
      for(;;) {const response=await api(`/api/videos/${entry.id}`,{signal});if(response.status!==202){info=response.data;break;}await wait(400,signal);}
      const mime=`video/mp4; codecs="${info.video_codec}"`;
      const mse=(window.MediaSource||window.ManagedMediaSource)?.isTypeSupported?.(mime)===true;
      const native=player.canPlayType('application/vnd.apple.mpegurl')!==''&&player.canPlayType(mime)!=='';
      if(!native&&(!window.Hls?.isSupported()||!mse))throw new Error(`This device cannot play ${info.bit_depth}-bit ${info.codec.toUpperCase()} video. Use a device that supports this codec. Video transcoding is disabled.`);
      if(navigator.mediaCapabilities?.decodingInfo) {
        let capability;
        try {capability=await navigator.mediaCapabilities.decodingInfo({type:native?'file':'media-source',video:{contentType:mime,width:info.width,height:info.height,bitrate:info.bitrate,framerate:Math.max(info.fps,1)}});} catch (_) {}
        if(capability?.supported===false)throw new Error(`This device does not support this video (${info.width} × ${info.height}, ${info.bit_depth}-bit ${info.codec.toUpperCase()}). Video transcoding is disabled.`);
      }
      const opus=!native&&info.audio==='opus'&&(window.MediaSource||window.ManagedMediaSource)?.isTypeSupported?.('audio/mp4; codecs="opus"')===true;
      if(!native&&info.audio&&!opus&&(window.MediaSource||window.ManagedMediaSource)?.isTypeSupported?.('audio/mp4; codecs="mp4a.40.2"')!==true)
        throw new Error('This device cannot decode the required AAC audio. Use a device with AAC support. Video transcoding is disabled.');
      for(;;) {
        const response=await api(`/api/videos/${entry.id}/sessions`,{method:'POST',signal,...json({supported:true,opus,open_id:viewing})});
        if(response.status!==202){current=response.data;break;}show('player-status',response.data.phase+'…');await wait(400,signal);
      }
      if(generation!==playNumber)return;
      $('player-meta').textContent=`${info.width} × ${info.height} · ${duration(info.duration)} · Original ${info.codec.toUpperCase()} video`;
      player.hidden=false;$('player-empty').hidden=true;$('fullscreen').hidden=false;show('player-status','Preparing the first segment…');
      if(native) {player.src=current.playlist;player.load();}
      else {
        hls=new Hls({maxBufferLength:20,maxMaxBufferLength:30,backBufferLength:30,maxBufferSize:32*1024**2,
          fragLoadingTimeOut:110000,manifestLoadingTimeOut:30000,enableWorker:true});
        hls.on(Hls.Events.ERROR,(_,data)=>{if(generation!==playNumber)return;if(data.fatal){const code=data.response?.code||data.networkDetails?.status;if(code===409){show('player-status','The source file changed. Refresh the library and open it again.',true);hls?.stopLoad();}else if(data.type===Hls.ErrorTypes.MEDIA_ERROR&&(recovering||recoverMedia()))return;else{show('player-status',`Playback failed: ${data.details}.`,true);hls?.stopLoad();}}});
        hls.loadSource(current.playlist);hls.attachMedia(player);
      }
      heartbeat=setInterval(()=>{samplePlayback();flushViewing();if(!player.seeking)touch(segmentAt(player.currentTime));},5000);
      player.play().catch(()=>{$('play-button').hidden=false;});
      if(innerWidth<760)$('player-title').closest('.player-panel').scrollIntoView({behavior:'smooth',block:'start'});
      document.querySelectorAll('.card-main').forEach(b=>{if(b.getAttribute('aria-label')===`Play ${entry.name}`)b.closest('.card').classList.add('selected');});
    } catch(error) {if(error.name!=='AbortError')show('player-status',error.message,true);}
  }
  function samplePlayback() {
    const now=performance.now()/1000, position=player.currentTime;
    if(playbackSample?.active&&viewing&&!player.seeking&&player.readyState>=2) {
      const elapsed=now-playbackSample.wall, advance=position-playbackSample.position;
      if(elapsed>0&&elapsed<=30&&advance>0&&advance<=elapsed*Math.max(player.playbackRate,1)+0.75) {
        const watched=Math.min(elapsed,advance/player.playbackRate), previous=viewedRanges.at(-1);
        if(previous&&Math.abs(previous[1]-playbackSample.position)<0.01&&previous[2]+watched<=30) {
          previous[1]=position;previous[2]+=watched;
        } else viewedRanges.push([playbackSample.position,position,watched]);
      }
    }
    playbackSample={wall:now,position,active:!player.paused&&!player.ended};
  }
  function flushViewing(closed=false) {
    if(viewing&&(viewedRanges.length||closed)) {
      const ranges=viewedRanges.splice(0), id=viewing, sequence=++viewingSequence;
      historyQueue.set(`${id}/${sequence}`,{id,payload:{sequence,ranges,closed},sending:false});
    }
    for(const [key,batch] of historyQueue) {
      if(batch.sending)continue;batch.sending=true;
      fetch(`/api/history/${batch.id}`,{method:'POST',keepalive:true,...json(batch.payload)})
        .then(response=>{if(response.ok||(response.status>=400&&response.status<500))historyQueue.delete(key);})
        .catch(()=>{}).finally(()=>{batch.sending=false;});
    }
  }
  function recoverMedia() {
    if(!hls||recovering||recoveries>=2)return false;
    recovering=true;recoveries++;show('player-status','Recovering the playback buffer…');
    hls.recoverMediaError();
    clearTimeout(recoveryTimer);
    recoveryTimer=setTimeout(()=>{
      recovering=false;
      if(!recoverMedia()){show('player-status','Playback could not recover. Reopen the video to retry.',true);hls?.stopLoad();}
    },30000);
    return true;
  }
  player.addEventListener('seeking',()=>{
    if(!current)return;playbackSample=null;flushViewing();clearTimeout(seekTimer);
    const target=player.currentTime, sequence=++seekNumber;lastSeek=performance.now();
    show('player-status','Seeking…');
    // hls.js owns init/appending state and cancels obsolete fragment loads itself.
    // Stopping its loader here can interrupt the first AV1/audio initialization.
    seekTimer=setTimeout(()=>touch(segmentAt(target),sequence),200);
  });
  player.addEventListener('timeupdate',samplePlayback);
  player.addEventListener('pause',()=>{samplePlayback();playbackSample=null;flushViewing();});
  player.addEventListener('ended',()=>{samplePlayback();playbackSample=null;flushViewing(true);});
  player.addEventListener('seeked',()=>{playbackSample=null;samplePlayback();});
  player.addEventListener('playing',()=>{recovering=false;clearTimeout(recoveryTimer);playbackSample=null;samplePlayback();show('player-status','');$('play-button').hidden=true;});
  player.addEventListener('canplay',()=>show('player-status',''));
  player.addEventListener('waiting',()=>{if(current)show('player-status','Buffering…');});
  player.addEventListener('error',()=>{
    if(!current||recovering)return;
    if(performance.now()-lastSeek<10000&&recoverMedia())return;
    show('player-status',`Playback error${player.error?.code?` (${player.error.code})`:''}. ${player.error?.message||'Unable to read or decode the media.'}`,true);
  });
  $('play-button').onclick=()=>player.play().catch(error=>show('player-status',error.message,true));
  $('fullscreen').onclick=()=>{if(player.requestFullscreen)player.requestFullscreen();else if(player.webkitEnterFullscreen)player.webkitEnterFullscreen();};
  $('close-player').onclick=stopPlayback;
  $('close-preview').onclick=()=>$('preview').close();
  $('preview').addEventListener('close',()=>{previewController?.abort();fetch('/api/previews/cancel',{method:'POST',...json({ids:[previewId],owner:previewOwner})}).catch(()=>{});$('preview-image').removeAttribute('src');});
  $('preview').addEventListener('click',event=>{if(event.target===$('preview')){const r=$('preview').getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)$('preview').close();}});
  $('search').addEventListener('input',()=>{const q=$('search').value.trim();clearTimeout(searchTimer);searchTimer=setTimeout(()=>navigate({mode:'items',q}),250);});
  $('refresh').onclick=async()=>{try{await api('/api/refresh',{method:'POST'});refreshPending=true;status();}catch(error){show('library-message',error.message,true);}};
  $('refresh-recommendations').onclick=()=>{
    if(route.mode!=='recommendations')return;
    const key=viewKey(route);views.delete(key);delete positions[key];viewState=null;loadView();
  };
  document.addEventListener('click',event=>{const a=event.target.closest('a[href^="#"]');if(!a||event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;event.preventDefault();saveView();if(location.hash===a.hash)loadView();else location.hash=a.hash||'#mode=directory';});
  $('load-status').onclick=()=>appendPage();
  window.addEventListener('hashchange',()=>loadView());
  window.addEventListener('scroll',()=>{clearTimeout(scrollTimer);scrollTimer=setTimeout(saveView,150);},{passive:true});
  let scrollTimer;
  window.addEventListener('pageshow',event=>{if(event.persisted)loadView();});
  window.addEventListener('pagehide',()=>{saveView();cancelView();stopPlayback();previewController?.abort();});
  status().then(()=>{if(!route)loadView();});setInterval(status,3000);
})();
