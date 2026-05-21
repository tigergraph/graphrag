"use client";

import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

import { LANGUAGES } from "../constants";

// TigerGraph's native sentinel for secret-based auth. pyTigerGraph
// handles this directly when sent as plain username/password.
const SECRET_USERNAME = "__GSQL__secret";

const WS_URL = "/ui/ui-login";

type TokenType = "apiToken" | "secret";

// Style applied to every credential input so Chrome on macOS doesn't
// clip descenders / underscores when rendering long values.
const INPUT_CLIP_FIX: React.CSSProperties = {
  WebkitAppearance: "none",
  appearance: "none",
  lineHeight: "1.5",
};

const INPUT_STYLE = "dark:border-[#3D3D3D] h-14 py-3 dark:bg-shadeA";

export function Login() {
  const { i18n, t } = useTranslation();
  const [useTokenLogin, setUseTokenLogin] = useState(false);
  const [tokenType, setTokenType] = useState<TokenType>("apiToken");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tokenValue, setTokenValue] = useState("");
  const [hint, setHint] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    setHint("");
  }, [useTokenLogin, tokenType, email, password, tokenValue]);

  const loginAction = async (e: React.FormEvent) => {
    e.preventDefault();

    // Build the full Authorization header value. API tokens use
    // ``Bearer <token>``; classic user/pass and TG ``__GSQL__secret``
    // logins use ``Basic <b64>``. Backend resolves the real TG
    // identity from SHOW USER and returns it in the response.
    let authHeader: string;
    let typedUsername = "";
    if (useTokenLogin) {
      const value = tokenValue.trim();
      if (value.length < 8) {
        setHint(
          tokenType === "apiToken"
            ? "Please enter an API token."
            : "Please enter a secret."
        );
        return;
      }
      if (tokenType === "apiToken") {
        authHeader = `Bearer ${value}`;
      } else {
        authHeader = `Basic ${btoa(`${SECRET_USERNAME}:${value}`)}`;
      }
    } else {
      const username = email.trim();
      if (username.length < 2 || password.length < 2) {
        setHint("Please enter your username and password.");
        return;
      }
      authHeader = `Basic ${btoa(`${username}:${password}`)}`;
      typedUsername = username;
    }

    setSubmitting(true);
    try {
      const res = await fetch(WS_URL, {
        method: "POST",
        headers: { Authorization: authHeader },
      });

      if (res.ok) {
        const data = await res.json();
        sessionStorage.setItem("auth", authHeader);
        sessionStorage.setItem("site", JSON.stringify(data));
        // Server-resolved username works in every mode; fall back to
        // the typed value for classic logins on older backends.
        const resolved =
          (typeof data.username === "string" && data.username) ||
          typedUsername;
        if (resolved) sessionStorage.setItem("username", resolved);
        else sessionStorage.removeItem("username");
        navigate("/chat");
      } else if (res.status === 401 || res.status === 403) {
        setHint(
          useTokenLogin ? "Invalid or unauthorized token." : "Invalid credentials."
        );
      } else {
        setHint(`Server error (${res.status}). Please try again later.`);
      }
    } catch {
      setHint("Unable to connect to the server. Please try again later.");
    } finally {
      setSubmitting(false);
    }
  };

  const onChangeLang = (e: React.ChangeEvent<HTMLSelectElement>) => {
    i18n.changeLanguage(e.target.value);
  };

  return (
    <>
      <img src="./tg-logo-xl.svg" className="mx-auto mb-5" />

      <h1 className="text-center text-2xl font-bold mb-5">
        {t("welcome")}
        <br />
        TigerGraph GraphRAG
      </h1>
      <h4 className="text-center mb-8 text-black dark:text-[#D9D9D9]">
        {t("login")}
      </h4>

      <form onSubmit={loginAction}>
        {useTokenLogin ? (
          <>
            <div className="mb-5">
              <label className="block text-sm font-medium mb-2 text-black dark:text-[#D9D9D9]">
                Credential type
              </label>
              <select
                value={tokenType}
                onChange={(e) => setTokenType(e.target.value as TokenType)}
                className="block w-full h-14 px-3 rounded-md border border-input bg-background dark:border-[#3D3D3D] dark:bg-shadeA text-sm"
              >
                <option value="apiToken">API Token</option>
                <option value="secret">Secret</option>
              </select>
            </div>
            <div className="mb-5">
              <Input
                placeholder={
                  tokenType === "apiToken" ? "API Token" : "Secret"
                }
                type="password"
                value={tokenValue}
                onChange={(e) => setTokenValue(e.target.value)}
                autoComplete="off"
                className={INPUT_STYLE}
                style={INPUT_CLIP_FIX}
              />
            </div>
          </>
        ) : (
          <>
            <div className="mb-5">
              <Input
                placeholder={t("username")}
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="username"
                className={INPUT_STYLE}
                style={INPUT_CLIP_FIX}
              />
            </div>
            <div className="mb-5">
              <Input
                placeholder={t("password")}
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                className={INPUT_STYLE}
                style={INPUT_CLIP_FIX}
              />
            </div>
          </>
        )}

        <div className="flex items-center space-x-2 mb-2">
          <input
            type="checkbox"
            id="useTokenLogin"
            className="rounded border-gray-300 dark:border-[#3D3D3D]"
            checked={useTokenLogin}
            onChange={(e) => {
              setUseTokenLogin(e.target.checked);
              setTokenValue("");
              setPassword("");
            }}
          />
          <label
            htmlFor="useTokenLogin"
            className="text-sm text-black dark:text-[#D9D9D9] cursor-pointer"
          >
            Use token login
          </label>
        </div>

        <Button
          type="submit"
          disabled={submitting}
          className="gradient w-full text-white mt-6"
        >
          {submitting ? "Signing in…" : t("submit")}
        </Button>

        <div className="inline-flex items-center justify-center w-full">
          <hr className="w-full h-px my-8 border-0 bg-gray-200 dark:bg-gray-700" />
          <span className="absolute px-3 text-xs bg-background dark:border-[#3D3D3D] text-gray-900 -translate-x-1/2 left-1/2 dark:text-white">
            {hint}
          </span>
        </div>
      </form>

      <select
        defaultValue={i18n.language}
        onChange={onChangeLang}
        className="rounded-lg border border-shadeA dark:bg-background dark:text-white absolute bottom-10 p-3 left-10"
      >
        {LANGUAGES.map(({ code, label }) => (
          <option key={code} value={code}>
            {label}
          </option>
        ))}
      </select>
    </>
  );
}
