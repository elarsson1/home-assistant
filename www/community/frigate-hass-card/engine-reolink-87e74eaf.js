import{l as e,e1 as t,e8 as a,e4 as n,ec as s,e9 as i,dN as r,d0 as o,e3 as c,e2 as l,ed as u,e6 as h,e7 as d,ea as g,d8 as m,da as _}from"./card-ab0d4006.js";import{B as p,a as y,i as f,g as w}from"./within-dates-ff02b695.js";import{C}from"./engine-86b0096c.js";import{p as M}from"./parse-a2496d43.js";import{e as k}from"./endOfDay-8d6ef2d2.js";import"./engine-generic-3bb78f34.js";import"./media-c9012082.js";class D extends t{}class b extends p{constructor(){super(...arguments),this._channel=null}async initialize(e){return await super.initialize(e),this._initializeChannel(),this}_initializeChannel(){const t=this._entity?.unique_id,a=t?String(t).match(/(.*)_(?<channel>\d+)/):null,n=a&&a.groups?.channel?Number(a.groups.channel):null;if(null===n)throw new D(e("error.camera_initialization_reolink"),this.getConfig());this._channel=n}getChannel(){return this._channel}getProxyConfig(){return{...super.getProxyConfig(),media:"auto"===this._config.proxy.media||this._config.proxy.media,ssl_verification:"auto"!==this._config.proxy.ssl_verification&&this._config.proxy.ssl_verification,ssl_ciphers:"auto"===this._config.proxy.ssl_ciphers?"intermediate":this._config.proxy.ssl_ciphers}}}class x{static isReolinkEventQueryResults(e){return e.engine===n.Reolink&&e.type===d.Event}}class v extends y{constructor(){super(...arguments),this._cache=new a}getEngineType(){return n.Reolink}_reolinkFileMetadataGenerator(e,t,a){
/* istanbul ignore next: This situation cannot happen as the directory would
        be rejected by _reolinkDirectoryMetadataGenerator if there was no start date
        -- @preserve */
if(!a?._metadata?.startDate||t.media_class!==s)return null;const n=t.title.split(/ +/);if(2!==n.length)return null;const o=M(n[0],"HH:mm:ss",a._metadata.startDate);if(!i(o))return null;const c=n[1].match(/(?<hours>\d+):(?<minutes>\d+):(?<seconds>\d+)/),l=c?.groups?{hours:Number(c.groups.hours),minutes:Number(c.groups.minutes),seconds:Number(c.groups.seconds)}:null;return{cameraID:e,startDate:o,endDate:l?r(o,l):o}}_reolinkDirectoryMetadataGenerator(e,t){const a=M(t.title,"yyyy/M/d",new Date);return i(a)?{cameraID:e,startDate:o(a),endDate:k(a)}:null}_reolinkCameraMetadataGenerator(e){const t=e.media_content_id.match(/^media-source:\/\/reolink\/CAM\|(?<configEntryID>.+)\|(?<channel>\d+)$/);return t?.groups?{configEntryID:t.groups.configEntryID,channel:Number(t.groups.channel)}:null}async createCamera(e,t){const a=new b(t,this,{capabilities:new c({"favorite-events":!1,"favorite-recordings":!1,clips:!0,live:!0,menu:!0,recordings:!1,seek:!1,snapshots:!1,substream:!0,ptz:l(t)??void 0},{disable:t.capabilities?.disable,disableExcept:t.capabilities?.disable_except}),eventCallback:this._eventCallback});return await a.initialize({entityRegistryManager:this._entityRegistryManager,hass:e,stateWatcher:this._stateWatcher})}async _getMatchingDirectories(e,t,a,n){const s=t.getConfig(),i=t.getEntity(),r=i?.config_entry_id;if(null===t.getChannel()||!r)return null;const o=await this._browseMediaManager.walkBrowseMedias(e,[{targets:["media-source://reolink"],metadataGenerator:(e,t)=>this._reolinkCameraMetadataGenerator(e),matcher:e=>e._metadata?.channel===t.getChannel()&&e._metadata?.configEntryID===r}],{...!1!==n?.useCache&&{cache:this._cache}});return o?.length?await this._browseMediaManager.walkBrowseMedias(e,[{targets:[`media-source://reolink/RES|${r}|${t.getChannel()}|`+("low"===s.reolink?.media_resolution?"sub":"main")],metadataGenerator:(e,a)=>this._reolinkDirectoryMetadataGenerator(t.getID(),e),matcher:e=>e.can_expand&&f(e,a?.start,a?.end),sorter:e=>u(e)}],{...!1!==n?.useCache&&{cache:this._cache}}):null}async getEvents(e,t,a,s){if(a.favorite||a.tags?.size||a.what?.size||a.where?.size||a.hasSnapshot)return null;const i=new Map,r=async r=>{const o={...a,cameraIDs:new Set([r])},c=s?.useCache??1?this._requestCache.get(o):null;if(c)return void i.set(o,c);const l=t.getCamera(r),h=l&&l instanceof b?await this._getMatchingDirectories(e,l,o,s):null,g=o.limit??C;let _=[];h?.length&&(_=await this._browseMediaManager.walkBrowseMedias(e,[{targets:h,concurrency:1,metadataGenerator:(e,t)=>this._reolinkFileMetadataGenerator(r,e,t),earlyExit:e=>e.length>=g,matcher:e=>!e.can_expand&&f(e,o.start,o.end),sorter:e=>u(e)}],{...!1!==s?.useCache&&{cache:this._cache}}));const p=m(_,(e=>e._metadata?.startDate),"desc").slice(0,g),y={type:d.Event,engine:n.Reolink,browseMedia:p};(s?.useCache??1)&&this._requestCache.set(o,{...y,cached:!0},y.expiry),i.set(o,y)};return await h(a.cameraIDs,(e=>r(e))),i}generateMediaFromEvents(e,t,a,n){return x.isReolinkEventQueryResults(n)?w(n.browseMedia):null}async getMediaMetadata(e,t,a,s){const i=new Map,o=s?.useCache??1?this._requestCache.get(a):null;if(o)return i.set(a,o),i;const c=new Set,l=async a=>{const n=t.getCamera(a);if(!(n&&n instanceof b))return;const i=await this._getMatchingDirectories(e,n,null,s);for(const e of i??[])
/* istanbul ignore next: This situation cannot happen as the directory
                will not match without metadata -- @preserve */
e._metadata&&c.add(_(e._metadata?.startDate))};await h(a.cameraIDs,(e=>l(e)));const u={type:d.MediaMetadata,engine:n.Reolink,metadata:{...c.size&&{days:c}},expiry:r(new Date,{seconds:g}),cached:!1};return(s?.useCache??1)&&this._requestCache.set(a,{...u,cached:!0},u.expiry),i.set(a,u),i}getCameraMetadata(e,t){return{...super.getCameraMetadata(e,t),engineIcon:"reolink"}}getCameraEndpoints(e,t){const a=e.reolink?.url?{endpoint:e.reolink.url}:null;return{...super.getCameraEndpoints(e,t),...a&&{ui:a}}}}export{v as ReolinkCameraManagerEngine,x as ReolinkQueryResultsClassifier};