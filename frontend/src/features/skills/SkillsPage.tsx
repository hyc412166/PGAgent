// 本文件实现 SkillsPage 功能域的页面或组件，并把接口数据、交互状态与公共展示组件连接起来。
import { BookOpen, Check, ChevronRight, Download, LoaderCircle, RefreshCw, Search, Trash2, Upload } from 'lucide-react'
import { Fragment, type FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api, describeError } from '../../api'
import { fallbackMarketCategories, leaderboardRefreshDelayMs, marketCategoryDefinitions, normalizeMarketCategories } from '../../skillMarketCategories'
import { EmptyState, ErrorState, LoadingState, PageHeader } from '../../components/ui'
import { useApiData } from '../../shared/hooks/useApiData'
import { formatDate } from '../../shared/lib/display'
import type { FolderSelection, SkillCatalogItem, SkillInstallPreview, SkillMarketplaceBrowse, SkillMarketplaceCategory, SkillMarketplaceItem, SkillMarketplaceLeaderboards, SkillMarketplaceSearch, SkillMarketplaceView } from '../../types'

// SkillsPage 同时管理已安装技能和远程市场，负责浏览、搜索、预览、安装、导入与删除链路。
function SkillsPage() {
  // installed 是本地权威目录；market 表示市场可用性，后续状态分别承载搜索和榜单数据。
  const installed = useApiData<SkillCatalogItem[]>([], () => api.list<SkillCatalogItem>('/api/skills', ['skills']), [])
  const market = useApiData<SkillMarketplaceSearch>({}, () => api.get<SkillMarketplaceSearch>('/api/skills/market/status'), [])
  // query/results 组成搜索状态，browse* 组成榜单状态，操作 ID 和 previews 负责逐卡片反馈。
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SkillMarketplaceItem[]>([])
  const [searchedQuery, setSearchedQuery] = useState('')
  const [browseView, setBrowseView] = useState<SkillMarketplaceView>('trending')
  const [browse, setBrowse] = useState<SkillMarketplaceBrowse>({})
  const [browsing, setBrowsing] = useState(false)
  const [browseError, setBrowseError] = useState('')
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState('')
  const [marketMessage, setMarketMessage] = useState('')
  const [importing, setImporting] = useState(false)
  const [installingId, setInstallingId] = useState('')
  const [deletingSkillId, setDeletingSkillId] = useState('')
  const [previews, setPreviews] = useState<Record<string, SkillInstallPreview>>({})
  const [actionError, setActionError] = useState('')
  // categoryBoards 及时间字段用于缓存分类榜，并按后端建议间隔执行后台刷新。
  const [categoryBoards, setCategoryBoards] = useState<SkillMarketplaceCategory[]>([])
  const [boardsLoading, setBoardsLoading] = useState(false)
  const [boardsRefreshing, setBoardsRefreshing] = useState(false)
  const [boardsError, setBoardsError] = useState('')
  const [boardsUpdatedAt, setBoardsUpdatedAt] = useState('')
  const [boardRefreshAfterSeconds, setBoardRefreshAfterSeconds] = useState(30 * 60)
  const browseRequestRef = useRef(0)
  const leaderboardRequestRef = useRef(0)

  const showingSearchResults = searchedQuery !== '' && searchedQuery === query.trim() && !searchError && !searching
  const activeMarketItems = showingSearchResults ? results : (browse.items ?? [])

  const loadBrowse = useCallback(async (view: SkillMarketplaceView, page = 0, append = false) => {
    const requestId = ++browseRequestRef.current
    setBrowsing(true); setBrowseError('')
    try {
      const response = await api.get<SkillMarketplaceBrowse>(`/api/skills/market/browse?view=${encodeURIComponent(view)}&page=${page}&per_page=12`)
      if (requestId === browseRequestRef.current) {
        setBrowse((current) => ({
          ...response,
          items: append ? [...(current.items ?? []), ...(response.items ?? [])] : (response.items ?? []),
        }))
      }
    } catch (error) {
      if (requestId === browseRequestRef.current) setBrowseError(describeError(error))
    } finally {
      if (requestId === browseRequestRef.current) setBrowsing(false)
    }
  }, [])

  useEffect(() => {
    if (market.data.available) void loadBrowse(browseView)
  }, [browseView, loadBrowse, market.data.available])

  const loadCategoryBoards = useCallback(async (manual = false) => {
    const requestId = ++leaderboardRequestRef.current
    if (manual) setBoardsRefreshing(true)
    else setBoardsLoading(true)
    setBoardsError('')
    try {
      let payload: SkillMarketplaceLeaderboards
      try {
        payload = manual
          ? await api.post<SkillMarketplaceLeaderboards>('/api/skills/market/leaderboards/refresh')
          : await api.get<SkillMarketplaceLeaderboards>('/api/skills/market/leaderboards')
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 404) throw error
        const entries = await Promise.all(marketCategoryDefinitions.map(async (category) => {
          const response = await api.post<SkillMarketplaceSearch>('/api/skills/market/search', { query: category.query, limit: 6 })
          return [category.id, response.items ?? []] as const
        }))
        payload = { categories: fallbackMarketCategories(Object.fromEntries(entries)) }
      }
      if (requestId === leaderboardRequestRef.current) {
        setCategoryBoards(normalizeMarketCategories(payload))
        setBoardsUpdatedAt(payload.updated_at || payload.refreshed_at || new Date().toISOString())
        setBoardRefreshAfterSeconds(payload.refresh_after_seconds || payload.refresh_interval_seconds || payload.ttl_seconds || 30 * 60)
      }
    } catch (error) {
      if (requestId === leaderboardRequestRef.current) setBoardsError(describeError(error))
    } finally {
      if (requestId === leaderboardRequestRef.current) {
        setBoardsLoading(false)
        setBoardsRefreshing(false)
      }
    }
  }, [])

  useEffect(() => {
    if (!market.data.available) return
    void loadCategoryBoards()
  }, [loadCategoryBoards, market.data.available])

  useEffect(() => {
    if (!market.data.available) return
    const timer = window.setInterval(() => void loadCategoryBoards(), leaderboardRefreshDelayMs(boardRefreshAfterSeconds))
    return () => window.clearInterval(timer)
  }, [boardRefreshAfterSeconds, loadCategoryBoards, market.data.available])

  async function importLocalSkill() {
    if (importing) return
    setImporting(true); setActionError('')
    try {
      const selection = await api.post<FolderSelection>('/api/system/select-folder', { title: 'Select Skill Folder' })
      if (!selection.path) return
      await api.post('/api/skills/import', { source_path: selection.path })
      await installed.reload()
    } catch (error) { setActionError(describeError(error)) } finally { setImporting(false) }
  }

  async function deleteInstalledSkill(skill: SkillCatalogItem) {
    if (deletingSkillId || !window.confirm(`确定删除 Skill“${skill.name}”吗？它会同时从 Agent 和会话的能力选择中移除，此操作不可撤销。`)) return
    setDeletingSkillId(skill.id); setActionError('')
    try {
      await api.delete(`/api/skills/${skill.id}`)
      await installed.reload()
    } catch (error) { setActionError(describeError(error)) } finally { setDeletingSkillId('') }
  }

  async function searchMarketQuery(normalizedQuery: string) {
    if (normalizedQuery.length < 2 || searching) return
    setSearching(true); setSearchError(''); setMarketMessage('')
    try {
      const response = await api.post<SkillMarketplaceSearch>('/api/skills/market/search', { query: normalizedQuery, limit: 20 })
      setResults(response.items ?? [])
      setSearchedQuery(normalizedQuery)
      setMarketMessage(response.message ?? '')
    } catch (error) {
      setResults([])
      setSearchedQuery('')
      setSearchError(describeError(error))
    } finally { setSearching(false) }
  }

  function searchMarket(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void searchMarketQuery(query.trim())
  }

  function chooseBrowseView(view: SkillMarketplaceView) {
    setResults([]); setQuery(''); setSearchedQuery(''); setSearchError(''); setMarketMessage('')
    if (view !== browseView) setBrowseView(view)
    else void loadBrowse(view)
  }

  async function previewMarketSkill(item: SkillMarketplaceItem) {
    if (installingId) return
    setInstallingId(item.id); setActionError('')
    try {
      const preview = await api.post<SkillInstallPreview>('/api/skills/market/install', { market_id: item.id, confirm: false })
      setPreviews((current) => ({ ...current, [item.id]: preview }))
    } catch (error) { setActionError(describeError(error)) } finally { setInstallingId('') }
  }

  async function confirmMarketSkill(item: SkillMarketplaceItem) {
    if (installingId || !previews[item.id]) return
    setInstallingId(item.id); setActionError('')
    try {
      await api.post('/api/skills/market/install', { market_id: item.id, confirm: true })
      setPreviews((current) => {
        const next = { ...current }
        delete next[item.id]
        return next
      })
      await installed.reload()
    } catch (error) { setActionError(describeError(error)) } finally { setInstallingId('') }
  }

  function showCategorySearch(category: SkillMarketplaceCategory) {
    const categoryQuery = category.query || category.label || ''
    setQuery(categoryQuery)
    void searchMarketQuery(categoryQuery)
    window.requestAnimationFrame(() => document.querySelector<HTMLInputElement>('.skill-search input')?.focus())
  }

  return <div className="page skills-page">
    <PageHeader eyebrow="能力冰库" title="技能库" description="整理已安装的 Skill，或从本地文件夹与在线市场补充新能力。" action={<button className="button button-primary" disabled={importing} onClick={() => void importLocalSkill()}>{importing ? <LoaderCircle className="spin" size={15} /> : <Upload size={15} />}导入本地 Skill</button>} />
    {actionError && <p className="form-error page-form-error" role="alert">{actionError}</p>}
    <section className="skills-section">
      <div className="skills-section-heading"><div><h2>已安装</h2><p>这里只显示后端实际返回的 Skill。</p></div><span>{installed.data.length}</span></div>
      {installed.error ? <ErrorState message={installed.error} onRetry={installed.reload} /> : installed.loading ? <LoadingState /> : installed.data.length ? <div className="skill-card-grid">
        {installed.data.map((skill) => <article className="skill-card" key={skill.id}>
          <div className="skill-card-icon"><BookOpen size={18} /></div>
          <div className="skill-card-copy"><div className="skill-card-title-row"><h3 title={skill.name}>{skill.name}</h3><button type="button" className="icon-button danger-icon skill-delete-button" aria-label={`删除 ${skill.name}`} title="删除 Skill" disabled={deletingSkillId === skill.id} onClick={() => void deleteInstalledSkill(skill)}>{deletingSkillId === skill.id ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}</button></div><p>{skill.description || '暂无说明'}</p></div>
          <dl><div><dt>版本</dt><dd>{skill.version || '未标注'}</dd></div><div><dt>来源</dt><dd title={skill.source_url || skill.source}>{skill.source || 'local'}</dd></div></dl>
        </article>)}
      </div> : <EmptyState icon={BookOpen} title="还没有安装 Skill" description="选择本地 Skill 文件夹导入，或在下方搜索在线市场。" />}
    </section>
    <section className="skills-section skill-market-section">
      <div className="skills-section-heading"><div><h2>在线市场</h2><p>{market.data.provider ? `来源：${market.data.provider} · 仅展示安装量，不代表活跃用户数。` : '搜索可下载的 Skill。'}</p></div></div>
      {market.error ? <ErrorState message={market.error} onRetry={market.reload} /> : market.loading ? <LoadingState label="正在检查市场服务" /> : market.data.available === false ? <ErrorState message={market.data.message || '在线市场当前不可用。'} onRetry={market.reload} /> : <>
        <section className="market-leaderboards" aria-label="按类型浏览热门 Skill">
          <header className="market-leaderboards-header">
            <div><strong>热门分类榜单</strong><small>{boardsUpdatedAt ? `更新于 ${formatDate(boardsUpdatedAt)} · 定期自动刷新` : '每类展示安装量最高的 6 个 Skill · 定期自动刷新'}</small></div>
            <button type="button" className="button button-secondary market-refresh-button" disabled={boardsLoading || boardsRefreshing} onClick={() => void loadCategoryBoards(true)}>{boardsLoading || boardsRefreshing ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}刷新榜单</button>
          </header>
          {boardsError ? <ErrorState message={boardsError} onRetry={() => void loadCategoryBoards(true)} /> : boardsLoading && !categoryBoards.length ? <LoadingState label="正在整理分类榜单" /> : <div className="market-category-grid">
            {categoryBoards.map((category) => <section className="market-category-card" key={category.id}>
              <header><div><h3>{category.label || category.id}</h3><p>{category.description || '热门可下载 Skill'}</p></div><button type="button" className="text-button" onClick={() => showCategorySearch(category)}>查看全部<ChevronRight size={13} /></button></header>
              {category.items?.length ? <ol>{category.items.slice(0, 6).map((item, index) => {
                const preview = previews[item.id]
                return <Fragment key={item.id}>
                  <li>
                    <span className="market-category-rank">{index + 1}</span><div><strong title={item.name}>{item.name}</strong><small title={item.slug || item.source || item.id}>{item.slug || item.source || item.id}</small></div><span className="market-category-installs">{typeof item.installs === 'number' ? `${item.installs.toLocaleString()} 次` : '—'}</span><button type="button" className="market-category-download" aria-label={`预览并下载 ${item.name}`} disabled={!!installingId} onClick={() => void previewMarketSkill(item)}>{installingId === item.id ? <LoaderCircle className="spin" size={13} /> : <Download size={13} />}</button>
                  </li>
                  {preview && <li className="market-category-preview"><span>已预览 {preview.files?.length ?? 0} 个文件</span><button type="button" className="button button-primary" disabled={!!installingId} onClick={() => void confirmMarketSkill(item)}>{installingId === item.id ? <LoaderCircle className="spin" size={13} /> : <Check size={13} />}确认导入</button></li>}
                </Fragment>
              })}</ol> : <p className="market-category-empty">暂无可展示的 Skill，刷新后再试。</p>}
            </section>)}
          </div>}
        </section>
        <form className="skill-search" onSubmit={searchMarket}>
          <Search size={16} /><input aria-label="搜索在线 Skill" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入至少 2 个字符搜索…" /><button className="button button-secondary" disabled={query.trim().length < 2 || searching}>{searching ? <LoaderCircle className="spin" size={14} /> : <Search size={14} />}搜索</button>
        </form>
        {searchError && <ErrorState message={searchError} onRetry={() => { const form = document.querySelector<HTMLFormElement>('.skill-search'); form?.requestSubmit() }} />}
        {!searchError && <div className="market-browser">
          <div className="market-browser-header"><strong>{showingSearchResults ? `搜索结果 · ${activeMarketItems.length}` : browse.total !== undefined && browse.total !== null ? `${browse.total.toLocaleString()} 个可浏览 Skill` : '发现 Skill'}</strong>{showingSearchResults && <button type="button" className="text-button" onClick={() => { setResults([]); setQuery(''); setSearchedQuery(''); void loadBrowse(browseView) }}>返回榜单</button>}</div>
          <div className="market-view-tabs" role="tablist" aria-label="Skill 市场榜单">
            {([{ id: 'trending', label: '趋势' }, { id: 'hot', label: '热度' }, { id: 'all-time', label: '热门' }, { id: 'curated', label: '官方精选' }] as Array<{ id: SkillMarketplaceView; label: string }>).map((tab) => <button key={tab.id} type="button" role="tab" aria-selected={!showingSearchResults && browseView === tab.id} className={!showingSearchResults && browseView === tab.id ? 'active' : ''} onClick={() => chooseBrowseView(tab.id)}>{tab.label}</button>)}
          </div>
        </div>}
        {marketMessage && <p className="market-message">{marketMessage}</p>}
        {browseError && !showingSearchResults && <ErrorState message={browseError} onRetry={() => void loadBrowse(browseView)} />}
        {(searching || (browsing && !activeMarketItems.length)) && <LoadingState label={searching ? '正在搜索 Skill' : '正在读取榜单'} />}
        {!searching && !browseError && activeMarketItems.length ? <div className="market-results">{activeMarketItems.map((item) => {
          const preview = previews[item.id]
          return <article key={item.id} className={preview ? 'has-preview' : ''}>
            <div><strong>{item.name}</strong><small>{item.slug || item.source || item.id}{item.is_duplicate ? ' · 重复来源' : ''}</small>{item.is_official && <em>官方精选{item.official_owner ? ` · ${item.official_owner}` : ''}</em>}</div>
            <span className="market-metrics">{typeof item.installs === 'number' && <b>{item.installs.toLocaleString()} 次安装</b>}{browseView === 'hot' && typeof item.change === 'number' && <small className={item.change > 0 ? 'positive' : ''}>{item.change >= 0 ? '+' : ''}{item.change} / 小时</small>}</span>
            <button className="button button-secondary" disabled={!!installingId} onClick={() => void previewMarketSkill(item)}>{installingId === item.id && !preview ? <LoaderCircle className="spin" size={14} /> : <Download size={14} />}{preview ? '重新预览' : '下载'}</button>
            {preview && <div className="skill-preview">
              <p><strong>来源</strong><span title={preview.source_url}>{preview.source_url}</span></p>
              <p><strong>候选目录</strong><span>{preview.candidates?.length ? preview.candidates.join('、') : '默认目录'}</span></p>
              <p><strong>文件</strong><span>{preview.files?.length ?? 0} 个</span></p>
              <button className="button button-primary" disabled={!!installingId} onClick={() => void confirmMarketSkill(item)}>{installingId === item.id ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}确认导入</button>
            </div>}
          </article>
        })}</div> : !searching && !browsing && !searchError && !browseError && <p className="market-empty">{showingSearchResults ? `未找到与“${searchedQuery}”匹配的 Skill。` : '这里会显示真实的市场结果。下载前会先展示文件清单，确认后才会导入。'}</p>}
        {!showingSearchResults && !browseError && browse.has_more && <div className="market-load-more"><button type="button" className="button button-secondary" disabled={browsing} onClick={() => void loadBrowse(browseView, (browse.page ?? 0) + 1, true)}>{browsing ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}加载更多</button></div>}
      </>}
    </section>
  </div>
}


export { SkillsPage }
