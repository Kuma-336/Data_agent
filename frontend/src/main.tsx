import { FormEvent, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

const API = 'http://localhost:8000';
type Chat = { id: string; title: string; updated_at: string };
type ChatMessage = { id?: string; role: string; content: string; parts?: Array<{ type: string; tool_name?: string; input?: unknown; output?: unknown; error?: string }> };
type Memory = { id: string; category: string; content: string };

async function api(path: string, options: RequestInit = {}) {
  const response = await fetch(`${API}${path}`, { credentials: 'include', ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  if (!response.ok) throw new Error(await response.text());
  return response;
}

function App() {
  const [user, setUser] = useState<{ email: string } | null>(null);
  const [email, setEmail] = useState(''); const [password, setPassword] = useState('');
  const [chats, setChats] = useState<Chat[]>([]); const [active, setActive] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]); const [prompt, setPrompt] = useState('');
  const [fileId, setFileId] = useState(''); const [isRunning, setIsRunning] = useState(false); const [error, setError] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false); const [memories, setMemories] = useState<Memory[]>([]); const [memoryEnabled, setMemoryEnabled] = useState(true);
  const fileInput = useRef<HTMLInputElement>(null);

  const loadChats = async () => { try { setChats(await (await api('/chats')).json()); } catch { /* session is not ready */ } };
  const loadMemories = async () => { try { setMemories(await (await api('/memories')).json()); } catch { /* session is not ready */ } };
  const openChat = async (id: string) => { const data = await (await api(`/chats/${id}`)).json(); setActive(id); setMessages(data.messages); setError(''); };
  const bootstrap = async () => { try { const me = await (await api('/auth/me')).json(); setUser(me); await Promise.all([loadChats(), loadMemories()]); } catch { setUser(null); } };
  useEffect(() => { void bootstrap(); }, []);

  async function authenticate(event: FormEvent, path: string) {
    event.preventDefault(); setError('');
    try { const me = await (await api(path, { method: 'POST', body: JSON.stringify({ email, password }) })).json(); setUser(me); await Promise.all([loadChats(), loadMemories()]); }
    catch (err) { setError(readError(err)); }
  }
  async function chooseFile(file?: File) {
    if (!file) return; setError(''); const form = new FormData(); form.append('upload', file);
    try { const response = await fetch(`${API}/files`, { method: 'POST', credentials: 'include', body: form }); if (!response.ok) throw new Error(await response.text()); const result = await response.json(); setFileId(result.id); }
    catch (err) { setError(readError(err)); }
  }
  async function createChat() { setActive(null); setMessages([]); setPrompt(''); setFileId(''); setError(''); }
  async function removeChat(id: string) { await api(`/chats/${id}`, { method: 'DELETE' }); if (id === active) createChat(); await loadChats(); }
  async function send(event: FormEvent) {
    event.preventDefault(); if (!prompt.trim() || isRunning) return; setIsRunning(true); setError('');
    const question = prompt.trim(); setPrompt(''); let chatId = active;
    try {
      if (!chatId) { const created = await (await api('/chats', { method: 'POST', body: JSON.stringify({ title: question.slice(0, 60) }) })).json(); chatId = created.id; setActive(chatId); await loadChats(); }
      if (!chatId) throw new Error('Unable to create chat');
      setMessages((current) => [...current, { role: 'user', content: question }]);
      const response = await api(`/chats/${chatId}/messages`, { method: 'POST', body: JSON.stringify({ content: question, file_ids: fileId ? [fileId] : [], connection_ids: [] }) });
      const reader = response.body?.getReader(); if (!reader) throw new Error('No response stream'); const decoder = new TextDecoder(); let buffer = ''; const toolParts: ChatMessage['parts'] = [];
      while (true) { const next = await reader.read(); if (next.done) break; buffer += decoder.decode(next.value, { stream: true }); const blocks = buffer.split('\n\n'); buffer = blocks.pop() || ''; for (const block of blocks) { const name = block.match(/^event: (.+)$/m)?.[1]; const raw = block.match(/^data: (.+)$/m)?.[1]; if (!name || !raw) continue; const data = JSON.parse(raw); if (name === 'tool') { toolParts.push({ type: 'tool', tool_name: data.name, input: data.input, output: data.output }); setMessages((current) => [...current.slice(0, -1), { ...current.at(-1)!, parts: [...toolParts] }]); } if (name === 'answer') setMessages((current) => [...current, { role: 'assistant', content: data.content, parts: toolParts }]); if (name === 'error') throw new Error(`${data.message} ${data.action ?? ''}`); } }
      await openChat(chatId); await loadChats(); await loadMemories();
    } catch (err) { setError(readError(err)); } finally { setIsRunning(false); }
  }
  async function toggleMemory() { const enabled = !memoryEnabled; await api('/settings/memory', { method: 'PATCH', body: JSON.stringify({ enabled }) }); setMemoryEnabled(enabled); }
  async function deleteMemory(id: string) { await api(`/memories/${id}`, { method: 'DELETE' }); await loadMemories(); }

  if (!user) return <Login email={email} password={password} setEmail={setEmail} setPassword={setPassword} authenticate={authenticate} error={error} />;
  return <div className="shell"><aside className="sidebar"><div className="brand"><span className="brand-mark">✦</span><span>data agent</span></div><button className="new-chat" onClick={createChat}><span>＋</span> New analysis</button><div className="side-label">CHATS</div><nav className="chat-list">{chats.map((chat) => <div className={`chat-nav ${chat.id === active ? 'selected' : ''}`} key={chat.id}><button onClick={() => void openChat(chat.id)}>{chat.title}</button><button className="ellipsis" onClick={() => void removeChat(chat.id)}>···</button></div>)}</nav><div className="side-spacer"/><button className="side-action" onClick={() => setSettingsOpen(true)}>⚙ Settings</button><div className="profile"><span className="avatar">{user.email[0].toUpperCase()}</span><span>{user.email}</span></div></aside><main className="chat-main"><header className="topbar"><div><span className="breadcrumb">My workspace</span><span className="chevron">/</span><strong>{active ? chats.find((chat) => chat.id === active)?.title ?? 'Analysis' : 'New analysis'}</strong></div><span className="status-dot">● Ready</span></header><section className="conversation">{messages.length === 0 ? <Welcome onExample={(text) => setPrompt(text)} /> : messages.map((message, index) => <MessageCard key={`${message.id ?? index}-${message.role}`} message={message} />)}{isRunning && <div className="thinking"><span/><span/><span/> Analyzing your data…</div>}</section><footer className="composer-wrap"><form className="composer" onSubmit={send}><button type="button" className="attach" onClick={() => fileInput.current?.click()} title="Attach CSV or XLSX">⌕</button><input ref={fileInput} type="file" accept=".csv,.xlsx" hidden onChange={(event) => void chooseFile(event.target.files?.[0])}/><textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Ask anything about your data…" rows={1}/><button className="send" disabled={isRunning || !prompt.trim()}>↑</button></form>{fileId && <div className="file-chip">▦ File attached <button onClick={() => setFileId('')}>×</button></div>}<p>Data Agent may make mistakes. Verify important results.</p></footer></main>{settingsOpen && <aside className="settings-panel"><div className="panel-heading"><div><span className="eyebrow">PERSONALIZATION</span><h2>Memory</h2></div><button onClick={() => setSettingsOpen(false)}>×</button></div><p className="muted">Stable preferences and business definitions are saved automatically. Credentials and raw data are excluded.</p><label className="toggle-row"><span>Automatic memory</span><button className={`toggle ${memoryEnabled ? 'on' : ''}`} onClick={() => void toggleMemory()}><i/></button></label><div className="memory-list">{memories.length === 0 ? <p className="muted">No saved memories yet.</p> : memories.map((memory) => <article key={memory.id}><small>{memory.category.replace('_', ' ')}</small><p>{memory.content}</p><button onClick={() => void deleteMemory(memory.id)}>Delete</button></article>)}</div></aside>} {error && <div className="toast"><strong>Request failed</strong><span>{error}</span><button onClick={() => setError('')}>×</button></div>}</div>;
}

function Login(props: { email: string; password: string; setEmail: (v: string) => void; setPassword: (v: string) => void; authenticate: (e: FormEvent, path: string) => Promise<void>; error: string }) { return <div className="login-page"><div className="login-card"><div className="brand"><span className="brand-mark">✦</span><span>data agent</span></div><h1>Welcome back</h1><p>Analyze files and databases with a dependable AI copilot.</p><form onSubmit={(event) => void props.authenticate(event, '/auth/login')}><label>Email<input type="email" value={props.email} onChange={(e) => props.setEmail(e.target.value)} required/></label><label>Password<input type="password" value={props.password} onChange={(e) => props.setPassword(e.target.value)} minLength={10} required/></label><button>Log in</button><button type="button" className="secondary" onClick={(event) => void props.authenticate(event as unknown as FormEvent, '/auth/register')}>Create account</button></form>{props.error && <p className="form-error">{props.error}</p>}</div></div>; }
function Welcome({ onExample }: { onExample: (text: string) => void }) { return <div className="welcome"><div className="welcome-icon">✦</div><h1>What would you like to analyze?</h1><p>Attach a CSV or XLSX file and ask a question in plain language.</p><div className="suggestions"><button onClick={() => onExample('Profile this dataset: explain the fields, quality issues, and important trends.')}>⌁ Profile this dataset</button><button onClick={() => onExample('Find the strongest patterns and outliers in this file.')}>↗ Find patterns and outliers</button><button onClick={() => onExample('Create an executive summary with the most important metrics.')}>▦ Create an executive summary</button></div></div>; }
function MessageCard({ message }: { message: ChatMessage }) { return <article className={`message ${message.role}`}><div className="message-label">{message.role === 'user' ? 'You' : 'Data Agent'}</div><div className="message-body">{message.content && <pre>{message.content}</pre>}{message.parts?.filter((part) => part.type === 'tool').map((part, index) => <details className="tool-card" key={index}><summary><span>⌘</span>{part.tool_name ?? 'Tool'}<small>{part.error ? 'Failed' : 'Complete'}</small></summary><pre>{JSON.stringify(part.error ?? part.output, null, 2)}</pre></details>)}</div></article>; }
function readError(error: unknown) { try { const parsed = JSON.parse(error instanceof Error ? error.message : String(error)); return `${parsed.message ?? 'Request failed'} ${parsed.action ?? ''}`; } catch { return error instanceof Error ? error.message : String(error); } }
createRoot(document.getElementById('root')!).render(<App/>);
