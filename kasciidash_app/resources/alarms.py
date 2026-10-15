"""UID-scoped alarm counts from the current snapshot; no per-row API calls."""
from collections import defaultdict
from kasciidash_app.resources import event_views

KINDS = {'pods':'Pod','deployments':'Deployment','replicasets':'ReplicaSet','statefulsets':'StatefulSet',
         'daemonsets':'DaemonSet','jobs':'Job','cronjobs':'CronJob','services':'Service','nodes':'Node',
         'namespaces':'Namespace'}


def group(version):
    return version.split('/')[0] if '/' in (version or '') else ''


def identity(obj):
    meta=obj.get('metadata') or {}
    return ('uid',meta['uid']) if meta.get('uid') else ('name',group(obj.get('apiVersion','')),obj.get('kind',''),meta.get('namespace') or '',meta.get('name',''))


def from_target(item):
    plural,_,api_group=item['resource'].partition('.')
    kind=item.get('kind') or KINDS.get(plural,'')
    version=item.get('apiVersion') or (api_group+'/v1' if api_group else 'apps/v1' if plural in ('deployments','replicasets','statefulsets','daemonsets') else 'v1')
    return {'apiVersion':version,'kind':kind,'metadata':{'uid':item.get('uid'),'name':item.get('name'),
            'namespace':item.get('namespace') if item.get('namespaced') else None}}


class Index:
    def __init__(self,data):
        self.available=data.get('events') is not None
        self.available_sources={key:data.get(key) is not None for key in ('pods','replicasets')}
        self.objects={};self.children=defaultdict(set);self.by_name=defaultdict(set)
        self.by_node=defaultdict(set);self.labels=defaultdict(set)
        self.rows_by_key=defaultdict(lambda:defaultdict(dict));self.rows_by_ns=defaultdict(lambda:defaultdict(dict))
        self.records={'warnings':{},'errors':{}};self.cache={}
        sources=[(name,data.get(name)) for name in ('nodes','pods','deployments','replicasets','statefulsets')]
        sources+=list((data.get('extras') or {}).items())
        for resource,rows in sources:
            for obj in rows or []:
                if not isinstance(obj,dict):continue
                meta=obj.get('metadata') or {};plural=resource.split('.')[0]
                enriched=dict(obj,kind=obj.get('kind') or KINDS.get(plural,''))
                if not enriched.get('apiVersion'):
                    suffix=resource.partition('.')[2]
                    enriched['apiVersion']=(suffix or ('apps' if plural in ('deployments','replicasets','statefulsets') else ''))+'/v1' if suffix or plural in ('deployments','replicasets','statefulsets') else 'v1'
                key=identity(enriched);self.objects[key]=enriched
                self.by_name[(group(enriched['apiVersion']),enriched['kind'],meta.get('namespace') or '',meta.get('name'))].add(key)
        for key,obj in self.objects.items():
            meta=obj.get('metadata') or {};ns=meta.get('namespace') or ''
            for ref in meta.get('ownerReferences') or []:
                if not isinstance(ref,dict):continue
                owner=self.resolve(dict(ref,namespace=ns))
                if owner is not None:self.children[owner].add(key)
            if obj['kind']=='Pod':
                self.by_node[(obj.get('spec') or {}).get('nodeName')].add(key)
                for label,value in (meta.get('labels') or {}).items():self.labels[(ns,label,str(value))].add(key)
        for event in data.get('events') or []:
            if not isinstance(event,dict):continue
            mode={'Warning':'warnings','Error':'errors'}.get(event.get('type'))
            if not mode:continue
            # Reuse event table formatting for Warning and nonstandard Error events.
            normalized=dict(event,type='Warning')
            rows=event_views.records({'events':[normalized]},'4')
            if rows:
                row=rows[0];row['raw']=event;ref=event.get('involvedObject') or event.get('regarding') or {}
                self.add(mode,row,ref)
        for row in event_views.records(data,'5',{}):
            ref=row.get('reference') or {}
            if ref:self.add('errors',row,ref)

    def resolve(self,ref):
        if ref.get('uid'):
            return ('uid',ref['uid'])
        candidates=self.by_name.get((group(ref.get('apiVersion','')),ref.get('kind',''),ref.get('namespace') or '',ref.get('name')),set())
        return next(iter(candidates)) if len(candidates)==1 else None

    def add(self,mode,row,ref):
        # Repeated samples of one retained Event are one alarm, not count occurrences.
        key=self.resolve(ref);rid=row['id']
        if isinstance(rid,tuple) and len(rid)==2 and isinstance(rid[1],int):rid=rid[0]
        self.records[mode][rid]=row
        if key is not None:self.rows_by_key[key][mode][rid]=row
        ns=ref.get('namespace') or row.get('namespace')
        if ns and ns!='-':self.rows_by_ns[ns][mode][rid]=row

    def related(self,obj):
        key=identity(obj);meta=obj.get('metadata') or {};kind=obj.get('kind')
        keys={key}
        if kind=='Service':
            selector=(obj.get('spec') or {}).get('selector') or {}
            candidates=None
            for label,value in selector.items():
                matches=self.labels.get((meta.get('namespace') or '',label,str(value)),set())
                candidates=set(matches) if candidates is None else candidates & matches
            keys.update(candidates or ())
        if kind=='Node':keys.update(self.by_node.get(meta.get('name'),()))
        todo=list(keys)
        while todo:
            for child in self.children.get(todo.pop(),()):
                if child not in keys:keys.add(child);todo.append(child)
        return keys

    def rows(self,obj,mode):
        if obj.get('kind')=='Namespace':
            records=self.rows_by_ns.get((obj.get('metadata') or {}).get('name'),{}).get(mode,{})
            return sorted(records.values(),key=lambda r:str(r.get('stamp','')),reverse=True)
        # Selectors can be absent/changed on the caller's snapshot; include them in the memo key.
        key=(identity(obj),obj.get('kind'),str((obj.get('spec') or {}).get('selector') or {}),mode)
        if key not in self.cache:
            rows={}
            for candidate in self.related(obj):rows.update(self.rows_by_key.get(candidate,{}).get(mode,{}))
            self.cache[key]=sorted(rows.values(),key=lambda r:str(r.get('stamp','')),reverse=True)
        return self.cache[key]

    def count(self,obj):return len(self.rows(obj,'warnings'))+len(self.rows(obj,'errors'))

    def complete(self,obj):
        kind=obj.get('kind')
        if not self.available:return False
        if kind in ('Deployment','ReplicaSet','StatefulSet','DaemonSet','Job','Service','Node') and not self.available_sources['pods']:return False
        return kind!='Deployment' or self.available_sources['replicasets']

    def label(self,obj):
        count=self.count(obj)
        return str(count) if self.complete(obj) else str(count)+'+?' if count else 'N/A'


def index(data):
    # Keep references in the cached index so object ids cannot be reused underneath it.
    sources=[data.get(k) for k in ('events','nodes','pods','deployments','replicasets','statefulsets','errors','catalog_errors')]
    extras=data.get('extras') or {};sources.extend(extras[k] for k in sorted(extras))
    key=(tuple(id(source) for source in sources),tuple(sorted(extras)),str(data.get('resource_errors') or {}))
    cached=data.get('_alarm_index')
    if cached is None or cached[0]!=key:
        value=Index(data);value.sources=sources
        data['_alarm_index']=(key,value)
    return data['_alarm_index'][1]


def detail_data(data,item,mode):
    value=index(data);obj=from_target(item)
    key=identity(obj)
    if not item.get('uid'):
        ref=dict(obj['metadata'],apiVersion=obj['apiVersion'],kind=obj['kind'])
        key=value.resolve(ref)
    obj=value.objects.get(key,obj)
    scope={'Namespace':'All retained records in namespace','Node':'Selected node + assigned Pods','Service':'Selected Service + selector-matching Pods (configuration, not observed traffic)'}.get(obj['kind'],'Selected resource + owned descendants')
    return {'rows':value.rows(obj,mode),'available':value.complete(obj),'scope':scope,'mode':mode}


def render(output,width,clean,table):
    rows=output['rows'];mode=output['mode']
    lines=[mode.upper()+' | '+str(len(rows))+' retained alarms','Scope: '+output['scope'],
           'Counts: distinct retained records; repeated Event count is not summed.']
    if not output['available']:lines+=['WARNING: Events or related resource collection unavailable; results may be incomplete.']
    if not rows:return lines+['','No related '+mode+'.']
    values=[[row.get('stamp','-'),row.get('namespace','-'),row.get('target','-'),row.get('reason','-'),row.get('message','')] for row in rows]
    return lines+['']+table(['LAST SEEN','NAMESPACE','OBJECT','REASON','MESSAGE'],values,width,max_widths={'OBJECT':40,'REASON':25,'MESSAGE':80})
