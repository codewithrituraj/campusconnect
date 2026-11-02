/* Supabase Auth client plus the Flask API client. */
(function () {
  const state = { client: null, session: null };
  const notify = () => window.dispatchEvent(new CustomEvent('cc:auth', { detail: state.session }));

  async function syncProfile(user, name) {
    if (!user?.id || !user?.email) return;
    // A confirmed signup may not have a session yet. The profile is created
    // after the first successful login in that case.
    if (!state.session?.access_token) return;
    const response = await fetch('/api/v1/users', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${state.session.access_token}`
      },
      body: JSON.stringify({ id: user.id, email: user.email, name: name || user.user_metadata?.name || user.email.split('@')[0] })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error?.message || 'Could not save the user profile');
    return payload.data;
  }

  async function init() {
    const configResponse = await fetch('/api/v1/config');
    const configPayload = await configResponse.json();
    if (!configResponse.ok) throw new Error(configPayload.error?.message || 'Backend configuration failed');
    state.client = window.supabase.createClient(configPayload.data.supabaseUrl, configPayload.data.supabaseAnonKey);
    const result = await state.client.auth.getSession();
    state.session = result.data.session;
    state.client.auth.onAuthStateChange((_event, session) => { state.session = session; notify(); });
    return state;
  }

  async function request(path, options = {}) {
    const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
    if (state.session?.access_token) headers.Authorization = `Bearer ${state.session.access_token}`;
    const response = await fetch(`/api/v1${path}`, { ...options, headers });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) { state.session = null; notify(); }
    if (!response.ok) throw new Error(payload.error?.message || 'Request failed');
    return payload.data;
  }

  async function signUp(email, password, metadata) {
    const result = await state.client.auth.signUp({ email, password, options: { data: metadata } });
    if (result.error) return result;
    state.session = result.data.session;
    const user = result.data.user;
    if (!user?.id || !user?.email) {
      throw new Error('Supabase created no user record. Please try again.');
    }
    // Auth signup is independent from the optional public profile row. When
    // email confirmation is enabled there is no session yet, so do not call
    // a protected backend endpoint here. The profile is created on first
    // login, after Supabase returns an access token.
    if (state.session?.access_token) {
      try { await syncProfile(user, metadata?.name); }
      catch (error) { console.warn('CampusConnect profile sync skipped:', error.message); }
    }
    notify();
    return result;
  }

  async function signIn(email, password) {
    const result = await state.client.auth.signInWithPassword({ email, password });
    if (!result.error) {
      state.session = result.data.session;
      try { await syncProfile(result.data.user, result.data.user?.user_metadata?.name); }
      catch (error) { console.warn('CampusConnect profile sync skipped:', error.message); }
      notify();
    }
    return result;
  }

  window.CampusConnectAPI = {
    init,
    request,
    get session() { return state.session; },
    signIn,
    signUp,
    signOut: async () => {
      const result = await state.client.auth.signOut();
      state.session = null;
      notify();
      return result;
    }
  };
})();
