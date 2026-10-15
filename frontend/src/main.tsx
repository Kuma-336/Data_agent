import { FormEvent, useEffect, useRef, useState } from 'react';
import type { ReactElement } from 'react';
import * as echarts from 'echarts';
import { createRoot } from 'react-dom/client';
import './styles.css';
import './charts.css';

const API = 'http://localhost:8000';
type Chat = { id: string; title: string; updated_at: string };
type ChatMessage = { id?: string; role: string; content: string; parts?: Array<{ type: string; tool_name?: string; input?: unknown; output?: unknown; error?: string }> };
type Memory = { id: string; category: string; content: string };
type ReportSummary = { id: string; title: string; chat_id: string | null; updated_at: string };
type ReportBlock = { id: string; type: 'markdown' | 'metric' | 'query_table' | 'chart' | 'divider'; content: Record<string, unknown>; query_id?: string | null; chart_id?: string | null; stale: boolean };
type Report = ReportSummary & { blocks: ReportBlock[] };
type Query = { columns: string[]; rows: Record<string, unknown>[] };
type SampleTable = { name: string; row_count: number; columns: Array<{ name: string; type: string }> };

async function api(path: string, options: RequestInit = {}) {
  const response = await fetch(`${API}${path}`, { credentials: 'include', ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  if (!response.ok) throw new Error(await response.text());
  return response;
}

function App() {
  const [currentUser, setUser] = useState<{ email: string } | null>(null);
  const [email, setEmail] = useState(''); const [password, setPassword] = useState('');
  const [chats, setChats] = useState<Chat[]>([]); const [active, setActive] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]); const [prompt, setPrompt] = useState('');
  const [fileId, setFileId] = useState(''); const [sampleTables, setSampleTables] = useState<SampleTable[]>([]); const [selectedSampleTables, setSelectedSampleTables] = useState<string[]>([]); const [attachmentPickerOpen, setAttachmentPickerOpen] = useState(false); const [isRunning, setIsRunning] = useState(false); const [error, setError] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false); const [memories, setMemories] = useState<Memory[]>([]); const [memoryEnabled, setMemoryEnabled] = useState(true);
  const [reports, setReports] = useState<ReportSummary[]>([]); const [activeReport, setActiveReport] = useState<Report | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const loadChats = async () => { try { setChats(await (await api('/chats')).json()); } catch { /* session is not ready */ } };
  const loadMemories = async () => { try { setMemories(await (await api('/memories')).json()); } catch { /* session is not ready */ } };
  const loadReports = async (chatId: string | null) => { if (!chatId) { setReports([]); return; } try { setReports(await (await api(`/reports?chat_id=${encodeURIComponent(chatId)}`)).json()); } catch { setReports([]); } };
  const openChat = async (id: string) => { const data = await (await api(`/chats/${id}`)).json(); setActive(id); setMessages(data.messages); setActiveReport(null); setError(''); await loadReports(id); };
  const openReport = async (reportId: string) => { try { setActiveReport(await (await api(`/reports/${reportId}`)).json()); setError(''); } catch (err) { setError(readError(err)); } };
  const loadSampleTables = async () => { try { setSampleTables(await (await api('/sample-tables/jaffle-shop')).json()); } catch { setSampleTables([]); } };
  const bootstrap = async () => { try { const me = await (await api('/auth/me')).json(); setUser(me); await Promise.all([loadChats(), loadMemories(), loadSampleTables()]); } catch { setUser(null); } };
  useEffect(() => { void bootstrap(); }, []);

  async function authenticate(event: FormEvent, path: string) {
    event.preventDefault(); setError('');
    try { const me = await (await api(path, { method: 'POST', body: JSON.stringify({ email, password }) })).json(); setUser(me); await Promise.all([loadChats(), loadMemories(), loadSampleTables()]); }
    catch (err) { setError(readError(err)); }
  }
  async function chooseFile(file?: File) {
    if (!file) return; setError(''); const form = new FormData(); form.append('upload', file);
    try { const response = await fetch(`${API}/files`, { method: 'POST', credentials: 'include', body: form }); if (!response.ok) throw new Error(await response.text()); const result = await response.json(); setFileId(result.id); setAttachmentPickerOpen(false); }
    catch (err) { setError(readError(err)); }
  }
  function toggleSampleTable(name: string) { setSelectedSampleTables((current) => current.includes(name) ? current.filter((item) => item !== name) : [...current, name]); setAttachmentPickerOpen(false); }
  async function createChat() { setActive(null); setMessages([]); setReports([]); setActiveReport(null); setPrompt(''); setFileId(''); setSelectedSampleTables([]); setAttachmentPickerOpen(false); setError(''); }
  async function removeChat(id: string) { await api(`/chats/${id}`, { method: 'DELETE' }); if (id === active) createChat(); await loadChats(); }
  async function send(event: FormEvent) {
    event.preventDefault(); if (!prompt.trim() || isRunning) return; setIsRunning(true); setError('');
    const question = prompt.trim(); setPrompt(''); let chatId = active;
    try {
      if (!chatId) { const created = await (await api('/chats', { method: 'POST', body: JSON.stringify({ title: question.slice(0, 60) }) })).json(); chatId = created.id; setActive(chatId); await loadChats(); }
      if (!chatId) throw new Error('Unable to create chat');
      setMessages((current) => [...current, { role: 'user', content: question }]);
      const response = await api(`/chats/${chatId}/messages`, { method: 'POST', body: JSON.stringify({ content: question, file_ids: fileId ? [fileId] : [], connection_ids: [], jaffle_shop_tables: selectedSampleTables }) });
      const reader = response.body?.getReader(); if (!reader) throw new Error('No response stream'); const decoder = new TextDecoder(); let buffer = ''; const toolParts: ChatMessage['parts'] = [];
      while (true) { const next = await reader.read(); if (next.done) break; buffer += decoder.decode(next.value, { stream: true }); const blocks = buffer.split('\n\n'); buffer = blocks.pop() || ''; for (const block of blocks) { const name = block.match(/^event: (.+)$/m)?.[1]; const raw = block.match(/^data: (.+)$/m)?.[1]; if (!name || !raw) continue; const data = JSON.parse(raw); if (name === 'tool') { toolParts.push({ type: 'tool', tool_name: data.name, input: data.input, output: data.output }); setMessages((current) => [...current.slice(0, -1), { ...current.at(-1)!, parts: [...toolParts] }]); } if (name === 'answer') setMessages((current) => [...current, { role: 'assistant', content: data.content, parts: toolParts }]); if (name === 'error') throw new Error(`${data.message} ${data.action ?? ''}`); } }
      await openChat(chatId); await loadChats(); await loadMemories(); await loadReports(chatId);
    } catch (err) { setError(readError(err)); } finally { setIsRunning(false); }
  }
  async function toggleMemory() { const enabled = !memoryEnabled; await api('/settings/memory', { method: 'PATCH', body: JSON.stringify({ enabled }) }); setMemoryEnabled(enabled); }
  async function deleteMemory(id: string) { await api(`/memories/${id}`, { method: 'DELETE' }); await loadMemories(); }

  if (!currentUser) return <Login email={email} password={password} setEmail={setEmail} setPassword={setPassword} authenticate={authenticate} error={error} />;
  const user = currentUser;
  const dashboard = (() => {
    const chatTitle = active ? chats.find((chat) => chat.id === active)?.title ?? 'Analysis' : 'New analysis';
    return <div className="shell">
      <aside className="sidebar"><div className="brand"><span className="brand-mark">✦</span><span>data agent</span></div><button className="new-chat" onClick={createChat}><span>＋</span> New analysis</button><div className="side-label">CHATS</div><nav className="chat-list">{chats.map((chat) => <div className={`chat-nav ${chat.id === active ? 'selected' : ''}`} key={chat.id}><button onClick={() => void openChat(chat.id)}>{chat.title}</button><button className="ellipsis" onClick={() => void removeChat(chat.id)} aria-label={`Delete ${chat.title}`}>···</button></div>)}</nav><section className="report-nav"><div className="side-label">REPORTS</div>{!active && <p className="side-empty">Select a chat to view its reports.</p>}{active && reports.length === 0 && <p className="side-empty">No reports in this session.</p>}{active && reports.map((report) => <button className={`report-nav-item ${activeReport?.id === report.id ? 'selected' : ''}`} key={report.id} onClick={() => void openReport(report.id)}><span>{report.title}</span><small>{formatDate(report.updated_at)}</small></button>)}</section><div className="side-spacer"/><button className="side-action" onClick={() => setSettingsOpen(true)}>⚙ Settings</button><div className="profile"><span className="avatar">{user!.email[0].toUpperCase()}</span><span>{user!.email}</span></div></aside>
      <main className="chat-main"><header className="topbar"><div><span className="breadcrumb">My workspace</span><span className="chevron">/</span><strong>{activeReport ? activeReport.title : chatTitle}</strong></div><span className="status-dot">● Ready</span></header>{activeReport ? <ReportView report={activeReport} onBack={() => setActiveReport(null)} /> : <><section className="conversation">{messages.length === 0 ? <Welcome onExample={setPrompt} /> : messages.map((message, index) => <MessageCard key={`${message.id ?? index}-${message.role}`} message={message} />)}{isRunning && <div className="thinking"><span/><span/><span/> Analyzing your data…</div>}</section><footer className="composer-wrap">{attachmentPickerOpen && <div className="attachment-picker"><button onClick={() => fileInput.current?.click()}>Upload CSV or XLSX</button><strong>jaffle_shop tables</strong><small>Select a table to attach it to this question.</small>{sampleTables.map((table) => <label key={table.name}><input type="checkbox" checked={selectedSampleTables.includes(table.name)} onChange={() => toggleSampleTable(table.name)}/><span><b>{table.name}</b><em>{table.row_count} rows · {table.columns.map((column) => column.name).join(', ')}</em></span></label>)}</div>}<div className="attachment-chips">{fileId && <div className="file-chip">▦ Uploaded file <button onClick={() => setFileId('')}>×</button></div>}{selectedSampleTables.map((name) => <div className="file-chip" key={name}>▣ jaffle_shop.{name} <button onClick={() => setSelectedSampleTables((tables) => tables.filter((table) => table !== name))}>×</button></div>)}</div><form className="composer" onSubmit={send}><button type="button" className="attach" onClick={() => setAttachmentPickerOpen((open) => !open)} title="Attach a file or table">⌕</button><input ref={fileInput} type="file" accept=".csv,.xlsx" hidden onChange={(event) => void chooseFile(event.target.files?.[0])}/><textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Ask anything about your data…" rows={1}/><button className="send" disabled={isRunning || !prompt.trim()}>↑</button></form><p>Data Agent may make mistakes. Verify important results.</p></footer></>}</main>
      {settingsOpen && <aside className="settings-panel"><div className="panel-heading"><div><span className="eyebrow">PERSONALIZATION</span><h2>Memory</h2></div><button onClick={() => setSettingsOpen(false)}>×</button></div><p className="muted">Stable preferences and business definitions are saved automatically. Credentials and raw data are excluded.</p><label className="toggle-row"><span>Automatic memory</span><button className={`toggle ${memoryEnabled ? 'on' : ''}`} onClick={() => void toggleMemory()}><i/></button></label><div className="memory-list">{memories.length === 0 ? <p className="muted">No saved memories yet.</p> : memories.map((memory) => <article key={memory.id}><small>{memory.category.replace('_', ' ')}</small><p>{memory.content}</p><button onClick={() => void deleteMemory(memory.id)}>Delete</button></article>)}</div></aside>}{error && <div className="toast"><strong>Request failed</strong><span>{error}</span><button onClick={() => setError('')}>×</button></div>}
    </div>;
  })();
  return dashboard;
  return <div className="shell"><aside className="sidebar"><div className="brand"><span className="brand-mark">✦</span><span>data agent</span></div><button className="new-chat" onClick={createChat}><span>＋</span> New analysis</button><div className="side-label">CHATS</div><nav className="chat-list">{chats.map((chat) => <div className={`chat-nav ${chat.id === active ? 'selected' : ''}`} key={chat.id}><button onClick={() => void openChat(chat.id)}>{chat.title}</button><button className="ellipsis" onClick={() => void removeChat(chat.id)}>···</button></div>)}</nav><div className="side-spacer"/><button className="side-action" onClick={() => setSettingsOpen(true)}>⚙ Settings</button><div className="profile"><span className="avatar">{user.email[0].toUpperCase()}</span><span>{user.email}</span></div></aside><main className="chat-main"><header className="topbar"><div><span className="breadcrumb">My workspace</span><span className="chevron">/</span><strong>{active ? chats.find((chat) => chat.id === active)?.title ?? 'Analysis' : 'New analysis'}</strong></div><span className="status-dot">● Ready</span></header><section className="conversation">{messages.length === 0 ? <Welcome onExample={(text) => setPrompt(text)} /> : messages.map((message, index) => <MessageCard key={`${message.id ?? index}-${message.role}`} message={message} />)}{isRunning && <div className="thinking"><span/><span/><span/> Analyzing your data…</div>}</section><footer className="composer-wrap"><form className="composer" onSubmit={send}><button type="button" className="attach" onClick={() => fileInput.current?.click()} title="Attach CSV or XLSX">⌕</button><input ref={fileInput} type="file" accept=".csv,.xlsx" hidden onChange={(event) => void chooseFile(event.target.files?.[0])}/><textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Ask anything about your data…" rows={1}/><button className="send" disabled={isRunning || !prompt.trim()}>↑</button></form>{fileId && <div className="file-chip">▦ File attached <button onClick={() => setFileId('')}>×</button></div>}<p>Data Agent may make mistakes. Verify important results.</p></footer></main>{settingsOpen && <aside className="settings-panel"><div className="panel-heading"><div><span className="eyebrow">PERSONALIZATION</span><h2>Memory</h2></div><button onClick={() => setSettingsOpen(false)}>×</button></div><p className="muted">Stable preferences and business definitions are saved automatically. Credentials and raw data are excluded.</p><label className="toggle-row"><span>Automatic memory</span><button className={`toggle ${memoryEnabled ? 'on' : ''}`} onClick={() => void toggleMemory()}><i/></button></label><div className="memory-list">{memories.length === 0 ? <p className="muted">No saved memories yet.</p> : memories.map((memory) => <article key={memory.id}><small>{memory.category.replace('_', ' ')}</small><p>{memory.content}</p><button onClick={() => void deleteMemory(memory.id)}>Delete</button></article>)}</div></aside>} {error && <div className="toast"><strong>Request failed</strong><span>{error}</span><button onClick={() => setError('')}>×</button></div>}</div>;
}

function ReportView({ report, onBack }: { report: Report; onBack: () => void }) {
  const hasNarrative = report.blocks.some((block) => block.type === 'markdown' && String(block.content.text ?? '').trim());
  return <section className="report-view"><div className="report-header"><button className="back-button" onClick={onBack}>← Back to chat</button><div className="report-actions"><span>Updated {formatDate(report.updated_at)}</span><a href={`${API}/reports/${report.id}/export?format=html`}>Download HTML</a><a href={`${API}/reports/${report.id}/export?format=pdf`}>Download PDF</a></div></div><h1>{report.title}</h1><div className="report-blocks">{!hasNarrative && report.chat_id && <ReportNarrative chatId={report.chat_id}/>} {report.blocks.map((block) => <ReportBlockView key={block.id} block={block} />)}</div></section>;
}
function ReportNarrative({ chatId }: { chatId: string }) {
  const [text, setText] = useState('');
  useEffect(() => { void (async () => { try { const chat = await (await api(`/chats/${chatId}`)).json() as { messages: ChatMessage[] }; const latest = [...chat.messages].reverse().find((message) => message.role === 'assistant' && message.content.trim()); setText(latest?.content ?? ''); } catch { setText(''); } })(); }, [chatId]);
  if (!text) return null;
  return <section className="report-narrative"><span className="report-eyebrow">ANALYSIS SUMMARY</span><MarkdownContent text={text}/></section>;
}
function ReportBlockView({ block }: { block: ReportBlock }) {
  if (block.stale) return <div className="report-stale">This referenced data is no longer available.</div>;
  if (block.type === 'markdown') return <MarkdownContent text={String(block.content.text ?? '')}/>;
  if (block.type === 'metric') return <article className="metric-card"><span>{String(block.content.label ?? 'Metric')}</span><strong>{String(block.content.value ?? '')}</strong></article>;
  if (block.type === 'divider') return <hr className="report-divider"/>;
  if (block.type === 'chart' && block.chart_id) return <ChartBlock chartId={block.chart_id}/>;
  if (block.type === 'query_table' && block.query_id) return <QueryTable queryId={block.query_id}/>;
  return <pre className="report-unknown">{JSON.stringify(block.content, null, 2)}</pre>;
}
function QueryTable({ queryId }: { queryId: string }) {
  const [query, setQuery] = useState<Query | null>(null); const [error, setError] = useState('');
  useEffect(() => { void (async () => { try { setQuery(await (await api(`/queries/${queryId}`)).json()); } catch (err) { setError(readError(err)); } })(); }, [queryId]);
  if (error) return <div className="report-stale">Unable to load table: {error}</div>;
  if (!query) return <p className="report-loading">Loading table…</p>;
  return <div className="report-table-wrap"><table className="report-table"><thead><tr>{query.columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{query.rows.map((row, index) => <tr key={index}>{query.columns.map((column) => <td key={column}>{String(row[column] ?? '')}</td>)}</tr>)}</tbody></table></div>;
}
function MarkdownContent({ text }: { text: string }) {
  const lines = text.split('\n'); const nodes: ReactElement[] = [];
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) { const content = inlineMarkdown(heading[2]); const key = index; const level = heading[1].length; nodes.push(level === 1 ? <h1 key={key}>{content}</h1> : level === 2 ? <h2 key={key}>{content}</h2> : level === 3 ? <h3 key={key}>{content}</h3> : <h4 key={key}>{content}</h4>); index += 1; continue; }
    if (/^-\s+/.test(line)) { const items: string[] = []; while (index < lines.length && /^-\s+/.test(lines[index])) { items.push(lines[index].replace(/^-\s+/, '')); index += 1; } nodes.push(<ul key={index}>{items.map((item, itemIndex) => <li key={itemIndex}>{inlineMarkdown(item)}</li>)}</ul>); continue; }
    if (/^\d+\.\s+/.test(line)) { const items: string[] = []; while (index < lines.length && /^\d+\.\s+/.test(lines[index])) { items.push(lines[index].replace(/^\d+\.\s+/, '')); index += 1; } nodes.push(<ol key={index}>{items.map((item, itemIndex) => <li key={itemIndex}>{inlineMarkdown(item)}</li>)}</ol>); continue; }
    const paragraph: string[] = []; while (index < lines.length && lines[index].trim() && !/^(#{1,4})\s+|^-\s+|^\d+\.\s+/.test(lines[index])) { paragraph.push(lines[index]); index += 1; } nodes.push(<p key={index}>{inlineMarkdown(paragraph.join(' '))}</p>);
  }
  return <div className="markdown-content">{nodes}</div>;
}
function inlineMarkdown(text: string) { return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean).map((part, index) => part.startsWith('**') ? <strong key={index}>{part.slice(2, -2)}</strong> : part.startsWith('`') ? <code key={index}>{part.slice(1, -1)}</code> : <span key={index}>{part}</span>); }
function formatDate(value: string) { const date = new Date(value); return Number.isNaN(date.valueOf()) ? '' : date.toLocaleString(); }
function Login(props: { email: string; password: string; setEmail: (v: string) => void; setPassword: (v: string) => void; authenticate: (e: FormEvent, path: string) => Promise<void>; error: string }) { return <div className="login-page"><div className="login-card"><div className="brand"><span className="brand-mark">✦</span><span>data agent</span></div><h1>Welcome back</h1><p>Analyze files and databases with a dependable AI copilot.</p><form onSubmit={(event) => void props.authenticate(event, '/auth/login')}><label>Email<input type="email" value={props.email} onChange={(e) => props.setEmail(e.target.value)} required/></label><label>Password<input type="password" value={props.password} onChange={(e) => props.setPassword(e.target.value)} minLength={10} required/></label><button>Log in</button><button type="button" className="secondary" onClick={(event) => void props.authenticate(event as unknown as FormEvent, '/auth/register')}>Create account</button></form>{props.error && <p className="form-error">{props.error}</p>}</div></div>; }
function Welcome({ onExample }: { onExample: (text: string) => void }) { return <div className="welcome"><div className="welcome-icon">✦</div><h1>What would you like to analyze?</h1><p>Attach a CSV or XLSX file and ask a question in plain language.</p><div className="suggestions"><button onClick={() => onExample('Profile this dataset: explain the fields, quality issues, and important trends.')}>⌁ Profile this dataset</button><button onClick={() => onExample('Find the strongest patterns and outliers in this file.')}>↗ Find patterns and outliers</button><button onClick={() => onExample('Create an executive summary with the most important metrics.')}>▦ Create an executive summary</button></div></div>; }
function MessageCard({ message }: { message: ChatMessage }) { return <article className={`message ${message.role}`}><div className="message-label">{message.role === 'user' ? 'You' : 'Data Agent'}</div><div className="message-body">{message.content && <MarkdownContent text={message.content}/>} {message.parts?.filter((part) => part.type === 'tool').map((part, index) => { const chartId = (part.output as { chart_id?: string } | undefined)?.chart_id; return <div key={index}>{chartId && <ChartBlock chartId={chartId}/>}<details className="tool-card"><summary><span>⌘</span>{part.tool_name ?? 'Tool'}<small>{part.error ? 'Failed' : 'Complete'}</small></summary><pre>{JSON.stringify(part.error ?? part.output, null, 2)}</pre></details></div>; })}</div></article>; }
function ChartBlock({ chartId }: { chartId: string }) { const ref = useRef<HTMLDivElement>(null); useEffect(() => { let chart: echarts.ECharts | undefined; void (async () => { const data = await (await api(`/charts/${chartId}`)).json(); if (!ref.current) return; const spec = data.spec; const rows = data.rows as Record<string, unknown>[]; chart = echarts.init(ref.current); if (spec.type === 'pie') chart.setOption({ title: { text: spec.title }, tooltip: {}, series: [{ type: 'pie', data: rows.map((row) => ({ name: String(row[spec.x_key]), value: row[spec.y_keys[0]] })) }] }); else chart.setOption({ title: { text: spec.title }, tooltip: {}, xAxis: { type: 'category', data: rows.map((row) => String(row[spec.x_key])) }, yAxis: { type: 'value' }, series: spec.y_keys.map((key: string) => ({ name: key, type: spec.type === 'area' ? 'line' : spec.type, areaStyle: spec.type === 'area' ? {} : undefined, data: rows.map((row) => row[key]) })) }); })(); return () => chart?.dispose(); }, [chartId]); return <div className="chart-block" ref={ref}/>; }
function readError(error: unknown) { try { const parsed = JSON.parse(error instanceof Error ? error.message : String(error)); return `${parsed.message ?? 'Request failed'} ${parsed.action ?? ''}`; } catch { return error instanceof Error ? error.message : String(error); } }
createRoot(document.getElementById('root')!).render(<App/>);
