--------------------------------------------------------------------------------
-- Enterprise AI Hub  |  DB upgrade 02  (SECURITY LOCK-DOWN)
--
-- !!! DO NOT RUN THIS UNTIL THE UPDATED ui_app.py (v2) IS INSTALLED !!!
-- After this script every /engine/... URL requires an OAuth token, so the current
-- (old) ui_app.py would get HTTP 401 on everything. App v2 reads ORDS_CLIENT_ID /
-- ORDS_CLIENT_SECRET from a local .env file and fetches the token automatically.
--
-- What it fixes:
--   * Today anyone on the internet can open .../engine/providers and read your API keys.
--   * Anyone can read any chat via .../engine/chat/messages/<id>.
--   * Login sends the password in the URL (?password=...) and passwords are stored as plain text.
--------------------------------------------------------------------------------
SET SERVEROUTPUT ON

--------------------------------------------------------------------------------
-- 1) Role + privilege: the whole 'AI' module now needs a token with this role
--------------------------------------------------------------------------------
DECLARE
    l_roles    OWA.VC_ARR;
    l_modules  OWA.VC_ARR;
    l_patterns OWA.VC_ARR;
BEGIN
    BEGIN
        ORDS.CREATE_ROLE(p_role_name => 'AI_HUB_SERVER');
    EXCEPTION WHEN OTHERS THEN NULL;   -- role already exists
    END;

    l_roles(1)   := 'AI_HUB_SERVER';
    l_modules(1) := 'AI';
    -- If your Employees / PRODUCTS data URLs live in ANOTHER ORDS module, add it here too, e.g.:
    -- l_modules(2) := 'HR_DATA';
    -- (find module names with:  SELECT name, uri_prefix FROM user_ords_modules;)

    ORDS.DEFINE_PRIVILEGE(
        p_privilege_name => 'ai.hub.engine',
        p_roles          => l_roles,
        p_patterns       => l_patterns,
        p_modules        => l_modules,
        p_label          => 'AI Hub engine',
        p_description    => 'Only the Enterprise AI Hub server may call these endpoints',
        p_comments       => NULL);
    COMMIT;
END;
/

--------------------------------------------------------------------------------
-- 2) OAuth client for the FastAPI server (client-credentials flow)
--    Copy CLIENT_ID and CLIENT_SECRET from the output into  F:\AI\AI 3\.env
--    The secret is shown ONCE. If you lose it, register a new secret.
--
--    UI alternative (if this block errors on your ORDS version):
--    Database Actions > REST > Security > OAuth Clients > Create
--      name: ai_hub_server | grant type: Client Credentials | role: AI_HUB_SERVER
--------------------------------------------------------------------------------
DECLARE
    l_cred ords_types.t_client_credentials;
BEGIN
    l_cred := ords_security.register_client(
        p_name            => 'ai_hub_server',
        p_grant_type      => 'client_credentials',
        p_description     => 'Enterprise AI Hub FastAPI server',
        p_support_email   => 'admin@example.com',     -- change to your email
        p_privilege_names => 'ai.hub.engine',
        p_token_duration  => 3600);
    ords_security.grant_client_role(
        p_client_key => l_cred.client_key,
        p_role_name  => 'AI_HUB_SERVER');
    COMMIT;
    DBMS_OUTPUT.PUT_LINE('ORDS_CLIENT_ID=' || l_cred.client_key.client_id);
    DBMS_OUTPUT.PUT_LINE('ORDS_CLIENT_SECRET=' || l_cred.client_secret.secret);
END;
/

--------------------------------------------------------------------------------
-- 3) Passwords: column wide enough for a PBKDF2 hash.
--    App v2 verifies passwords itself (PBKDF2-SHA256, 200k rounds) and upgrades each
--    plain-text password to a hash automatically at that user's next successful login.
--------------------------------------------------------------------------------
DECLARE
    l_len NUMBER;
BEGIN
    SELECT data_length INTO l_len FROM user_tab_columns
     WHERE table_name = 'AI_USERS' AND column_name = 'PASSWORD_HASH';
    IF l_len < 255 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE ai_users MODIFY (password_hash VARCHAR2(255))';
    END IF;
END;
/

--------------------------------------------------------------------------------
-- 4) New auth handlers (POST body, never in the URL). Only the server can call them now.
--------------------------------------------------------------------------------
BEGIN
    -- POST auth/user {username} -> the stored hash; the app checks the password itself
    ORDS.DEFINE_TEMPLATE(p_module_name => 'AI', p_pattern => 'auth/user');
    ORDS.DEFINE_HANDLER(
        p_module_name => 'AI', p_pattern => 'auth/user', p_method => 'POST',
        p_source_type => 'plsql/block',
        p_source      => q'[
DECLARE
    l_out CLOB;
BEGIN
    SELECT JSON_OBJECT('user_id' VALUE user_id, 'username' VALUE username,
                       'password_hash' VALUE password_hash, 'is_admin' VALUE NVL(is_admin, 'N'),
                       'daily_token_quota' VALUE daily_token_quota)
      INTO l_out
      FROM ai_users
     WHERE LOWER(username) = LOWER(:username) AND is_active = 'Y';
    OWA_UTIL.MIME_HEADER('application/json', TRUE);
    HTP.P(l_out);
EXCEPTION WHEN NO_DATA_FOUND THEN
    OWA_UTIL.MIME_HEADER('application/json', TRUE);
    HTP.P('{}');
END;]');

    -- POST auth/set-password {user_id, password_hash}
    ORDS.DEFINE_TEMPLATE(p_module_name => 'AI', p_pattern => 'auth/set-password');
    ORDS.DEFINE_HANDLER(
        p_module_name => 'AI', p_pattern => 'auth/set-password', p_method => 'POST',
        p_source_type => 'plsql/block',
        p_source      => q'[
BEGIN
    UPDATE ai_users SET password_hash = :password_hash WHERE user_id = TO_NUMBER(:user_id);
END;]');
    COMMIT;
END;
/

--------------------------------------------------------------------------------
-- 5) After app v2 logs in fine, delete the old GET auth/login handler
--    (APEX > SQL Workshop > RESTful Services > Modules > AI > auth/login > Delete)
--    and ROTATE your Groq / OpenRouter API keys (they have been publicly readable).
--------------------------------------------------------------------------------
