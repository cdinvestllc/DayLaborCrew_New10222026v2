import React, { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import axios from "axios";
import { toast } from "sonner";
import { CheckCircle, Coins, CreditCard, Loader2, RefreshCw, ShieldCheck, Wallet } from "lucide-react";
import Navbar from "../components/Navbar";
import { useAuth } from "../contexts/AuthContext";

const API = `${process.env.REACT_APP_BACKEND_URL}/api/billing`;

function newRequestKey() {
  if (typeof window !== "undefined" && window.crypto?.randomUUID) {
    return `web-${window.crypto.randomUUID()}`;
  }
  return `web-${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

function money(cents, currency = "usd") {
  return new Intl.NumberFormat("en-US", {
    style: "currency", currency: (currency || "usd").toUpperCase()
  }).format((Number(cents) || 0) / 100);
}

export default function SubscriptionPage() {
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const [catalog, setCatalog] = useState(null);
  const [wallet, setWallet] = useState({ balance: 0, lots: [] });
  const [interval, setInterval] = useState("month");
  const [provider, setProvider] = useState("stripe");
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const requestKeys = useRef(new Map());
  const handledReturn = useRef(null);

  const planId = user?.role === "contractor" ? "pro_contractor" : "pro_crew";
  const plan = catalog?.catalog?.plans?.[planId];
  const tokenPack = catalog?.catalog?.token_pack;
  const providerEnabled = Boolean(catalog?.catalog?.payment_providers?.[provider]);

  const refreshData = useCallback(async () => {
    setRefreshing(true);
    try {
      const [catalogResponse, walletResponse] = await Promise.all([
        axios.get(`${API}/catalog`),
        axios.get(`${API}/wallet`)
      ]);
      setCatalog(catalogResponse.data);
      setWallet(walletResponse.data);
    } catch (error) {
      toast.error(error?.response?.data?.detail || "Billing information is temporarily unavailable.");
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { refreshData(); }, [refreshData]);

  const pollBillingReturn = useCallback(async () => {
    const returnType = params.get("billing_return");
    const sessionId = params.get("session_id");
    const subscriptionId = params.get("billing_subscription_id");
    const paymentId = params.get("billing_payment_id");
    const cancelRequested = params.get("billing_cancelled");
    const returnKey = `${returnType || ""}:${sessionId || ""}:${subscriptionId || ""}:${paymentId || ""}:${cancelRequested || ""}`;
    if (handledReturn.current === returnKey || !(returnType || cancelRequested)) return;
    handledReturn.current = returnKey;

    if (cancelRequested === "1") {
      toast.message("Checkout was cancelled; no tokens were granted.");
      setParams(current => {
        const next = new URLSearchParams(current);
        next.delete("billing_cancelled");
        next.delete("billing_return");
        next.delete("session_id");
        next.delete("billing_subscription_id");
        next.delete("billing_payment_id");
        return next;
      }, { replace: true });
      return;
    }

    setLoading(true);
    try {
      if (returnType === "paypal-pack" && paymentId) {
        const result = await axios.post(`${API}/payments/paypal/token-pack-capture-payment/${encodeURIComponent(paymentId)}`);
        if (result.data.payment_status !== "fulfilled" && result.data.status !== "COMPLETED") {
          throw new Error("Payment is not complete yet. The wallet has not been credited.");
        }
      } else if (returnType === "stripe-subscription" || returnType === "stripe-pack") {
        if (!sessionId) throw new Error("The checkout session reference is missing.");
        let confirmed = false;
        for (let attempt = 0; attempt < 10; attempt += 1) {
          const status = await axios.get(`${API}/payments/stripe/status/${encodeURIComponent(sessionId)}`);
          if (returnType === "stripe-pack" && status.data.payment_status === "paid" && status.data.tokens_granted) {
            confirmed = true;
            break;
          }
          if (returnType === "stripe-subscription" && status.data.payment_status === "paid" && status.data.tokens_granted) {
            confirmed = true;
            break;
          }
          if (status.data.payment_status === "verification_failed") {
            throw new Error("Payment could not be verified. No tokens were granted.");
          }
          await new Promise(resolve => setTimeout(resolve, 1500));
        }
        if (!confirmed) throw new Error("Payment verification is still processing. Refresh your wallet shortly.");
      } else if (returnType === "paypal-subscription" && subscriptionId) {
        let confirmed = false;
        for (let attempt = 0; attempt < 10; attempt += 1) {
          const status = await axios.get(`${API}/payments/paypal/subscription-status/${encodeURIComponent(subscriptionId)}`);
          if (status.data.tokens_granted) {
            confirmed = true;
            break;
          }
          if (["cancelled", "expired", "suspended"].includes(status.data.status)) {
            throw new Error("The PayPal subscription is not active. No tokens were granted.");
          }
          await new Promise(resolve => setTimeout(resolve, 1500));
        }
        if (!confirmed) throw new Error("PayPal payment is still processing. Tokens will appear after the verified payment event.");
      } else {
        throw new Error("The returned billing session could not be identified.");
      }
      await refreshData();
      toast.success("Payment verified. Your token wallet has been updated.");
    } catch (error) {
      toast.error(error?.response?.data?.detail || error.message || "Payment is processing; no unverified tokens were added.");
      await refreshData();
    } finally {
      setLoading(false);
      setParams(current => {
        const next = new URLSearchParams(current);
        ["billing_return", "session_id", "billing_subscription_id", "billing_payment_id", "billing_cancelled"].forEach(key => next.delete(key));
        return next;
      }, { replace: true });
    }
  }, [params, refreshData, setParams]);

  useEffect(() => { pollBillingReturn(); }, [pollBillingReturn]);

  const stableRequestKey = (operation) => {
    const key = `${operation}:${provider}:${interval}`;
    if (!requestKeys.current.has(key)) requestKeys.current.set(key, newRequestKey());
    return requestKeys.current.get(key);
  };

  const startSubscription = async () => {
    if (!providerEnabled) {
      toast.error(`${provider.replace("_", " ")} payments are not enabled yet.`);
      return;
    }
    if (!plan) {
      toast.error("The subscription catalog has not loaded.");
      return;
    }
    setLoading(true);
    try {
      const endpoint = provider === "stripe"
        ? `${API}/payments/stripe/subscription-session`
        : `${API}/payments/paypal/subscription`;
      const response = await axios.post(endpoint, {
        interval,
        origin_url: window.location.origin,
        request_key: stableRequestKey("subscription")
      });
      const url = response.data.url || response.data.approve_url;
      if (!url) throw new Error("The payment provider did not return a checkout URL.");
      window.location.assign(url);
    } catch (error) {
      toast.error(error?.response?.data?.detail || error.message || "Could not start subscription checkout.");
      setLoading(false);
    }
  };

  const buyTokenPack = async () => {
    if (!providerEnabled || !tokenPack?.enabled) {
      toast.error("Token pack checkout is not available with this payment method.");
      return;
    }
    setLoading(true);
    try {
      const endpoint = provider === "stripe"
        ? `${API}/payments/stripe/token-pack-session`
        : `${API}/payments/paypal/token-pack-order`;
      const response = await axios.post(endpoint, {
        origin_url: window.location.origin,
        request_key: stableRequestKey("token-pack")
      });
      const url = response.data.url || response.data.approve_url;
      if (!url) throw new Error("The payment provider did not return a checkout URL.");
      window.location.assign(url);
    } catch (error) {
      toast.error(error?.response?.data?.detail || error.message || "Could not start token pack checkout.");
      setLoading(false);
    }
  };

  const activeLots = (wallet.lots || []).filter(lot => Number(lot.remaining) > 0);

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-[#020617]" style={{ fontFamily: "Inter, sans-serif" }}>
      <Navbar />
      <main className="max-w-5xl mx-auto px-4 py-10">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between mb-8">
          <div>
            <h1 className="text-3xl sm:text-4xl font-extrabold text-[#050A30] dark:text-white mb-2"
              style={{ fontFamily: "Manrope, sans-serif" }}>Tokens & Membership</h1>
            <p className="text-slate-600 dark:text-slate-400">
              Payments are verified server-side. Tokens are added only after confirmation by the payment provider.
            </p>
          </div>
          <button onClick={refreshData} disabled={refreshing || loading}
            className="inline-flex items-center justify-center gap-2 rounded-xl border border-slate-300 dark:border-slate-700 px-4 py-2 text-sm font-semibold text-slate-700 dark:text-slate-200 disabled:opacity-50">
            {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            Refresh wallet
          </button>
        </div>

        <section className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
          <div className="md:col-span-1 rounded-2xl bg-white dark:bg-slate-900 p-6 shadow-sm border border-slate-200 dark:border-slate-800">
            <div className="flex items-center gap-2 text-slate-500 dark:text-slate-400 text-sm mb-3">
              <Wallet className="h-4 w-4" /> Available tokens
            </div>
            <div className="text-4xl font-extrabold text-[#0000FF] dark:text-blue-300">{wallet.balance ?? 0}</div>
            <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">Spendable balance after expired lots are removed.</p>
            <div className="mt-5 space-y-3">
              {activeLots.length === 0 ? <p className="text-sm text-slate-500">No active token grants yet.</p> : activeLots.slice(0, 4).map(lot => (
                <div key={lot.lot_id} className="flex justify-between gap-3 text-sm border-t border-slate-100 dark:border-slate-800 pt-3">
                  <div>
                    <p className="font-semibold text-slate-700 dark:text-slate-200 capitalize">{String(lot.source || "tokens").replaceAll("_", " ")}</p>
                    <p className="text-xs text-slate-500">Expires {lot.expires_at ? new Date(lot.expires_at).toLocaleDateString() : "—"}</p>
                  </div>
                  <span className="font-bold tabular-nums">{lot.remaining}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="md:col-span-2 rounded-2xl bg-white dark:bg-slate-900 p-6 shadow-sm border border-slate-200 dark:border-slate-800">
            <div className="flex items-center gap-2 mb-4">
              <ShieldCheck className="h-5 w-5 text-[#0000FF] dark:text-blue-300" />
              <h2 className="text-xl font-bold text-slate-900 dark:text-white">Pro membership</h2>
            </div>
            {plan ? <>
              <p className="text-sm text-slate-500 dark:text-slate-400 mb-2">{user?.role === "contractor" ? "Pro Contractor" : "Pro Crew"}</p>
              <div className="flex flex-wrap items-baseline gap-2 mb-4">
                <span className="text-4xl font-extrabold text-[#0000FF] dark:text-blue-300">
                  {money(interval === "year" ? plan.annual_price_cents : plan.monthly_price_cents, catalog?.catalog?.currency)}
                </span>
                <span className="text-slate-500 dark:text-slate-400">/{interval === "year" ? "year" : "month"}</span>
              </div>
              <p className="font-semibold text-slate-800 dark:text-slate-100 mb-3">
                {plan.monthly_tokens} tokens every month
              </p>
              <div className="flex gap-2 mb-5" role="group" aria-label="Billing interval">
                <button onClick={() => setInterval("month")}
                  className={`rounded-lg px-4 py-2 border text-sm font-semibold ${interval === "month" ? "bg-blue-700 border-blue-700 text-white" : "border-slate-300 dark:border-slate-700 text-slate-700 dark:text-slate-200"}`}>
                  Monthly
                </button>
                <button onClick={() => setInterval("year")}
                  className={`rounded-lg px-4 py-2 border text-sm font-semibold ${interval === "year" ? "bg-blue-700 border-blue-700 text-white" : "border-slate-300 dark:border-slate-700 text-slate-700 dark:text-slate-200"}`}>
                  Annual
                </button>
              </div>
              <div className="flex flex-wrap items-center gap-3 mb-4">
                <label htmlFor="billing-provider" className="text-sm font-semibold text-slate-700 dark:text-slate-200">Payment method</label>
                <select id="billing-provider" value={provider} onChange={event => setProvider(event.target.value)}
                  className="rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-950 px-3 py-2 text-sm text-slate-800 dark:text-slate-100">
                  <option value="stripe" disabled={!catalog?.catalog?.payment_providers?.stripe}>Card / Stripe</option>
                  <option value="paypal" disabled={!catalog?.catalog?.payment_providers?.paypal}>PayPal</option>
                </select>
              </div>
              <button onClick={startSubscription} disabled={loading || refreshing || !catalog || !providerEnabled}
                className="inline-flex w-full sm:w-auto justify-center items-center gap-2 rounded-xl bg-blue-700 hover:bg-blue-800 text-white font-bold px-5 py-3 disabled:opacity-50">
                {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <CreditCard className="h-4 w-4" />}
                Continue to secure checkout
              </button>
              <p className="mt-3 text-xs text-slate-500 dark:text-slate-400">
                Annual subscribers pay upfront; included monthly tokens are released on a monthly schedule. Prices are snapshots taken when checkout starts.
              </p>
            </> : <p className="text-sm text-slate-500">Loading the current billing catalog…</p>}
          </div>
        </section>

        <section className="rounded-2xl bg-white dark:bg-slate-900 p-6 shadow-sm border border-slate-200 dark:border-slate-800 mb-8">
          <div className="flex items-center gap-2 mb-2">
            <Coins className="h-5 w-5 text-[#0000FF] dark:text-blue-300" />
            <h2 className="text-xl font-bold text-slate-900 dark:text-white">Token pack</h2>
          </div>
          <p className="text-slate-600 dark:text-slate-400 mb-4">
            Buy a one-time token pack. The wallet ledger records the purchase and its expiration date.
          </p>
          {tokenPack ? <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
            <div>
              <p className="text-2xl font-extrabold text-slate-900 dark:text-white">{tokenPack.tokens} tokens</p>
              <p className="text-lg font-semibold text-[#0000FF] dark:text-blue-300">
                {money(tokenPack.price_cents, catalog?.catalog?.currency)}
              </p>
            </div>
            <button onClick={buyTokenPack} disabled={loading || refreshing || !tokenPack.enabled || !providerEnabled}
              className="inline-flex justify-center items-center gap-2 rounded-xl border-2 border-blue-700 text-blue-700 dark:text-blue-300 dark:border-blue-400 hover:bg-blue-50 dark:hover:bg-slate-800 font-bold px-5 py-3 disabled:opacity-50">
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <CreditCard className="h-4 w-4" />}
              Buy token pack
            </button>
          </div> : <p className="text-sm text-slate-500">Loading token pack price…</p>}
        </section>

        <section className="rounded-2xl border border-slate-200 dark:border-slate-800 p-5 text-sm text-slate-600 dark:text-slate-400">
          <div className="flex items-start gap-2">
            <CheckCircle className="h-5 w-5 shrink-0 text-emerald-600" />
            <p>Payment confirmation and token granting are separate steps. Unpaid, mismatched, or unverified provider events do not credit the wallet. Points are separate from tokens. Expiry dates are visible in your wallet above.</p>
          </div>
        </section>
      </main>
    </div>
  );
}
