import React, { useEffect, useRef, useState } from "react";
import { X, Zap, CreditCard, DollarSign, CheckCircle, ExternalLink } from "lucide-react";
import axios from "axios";
import { toast } from "sonner";
import { useAuth } from "../contexts/AuthContext";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const BOOST_PRICE = 9.99;
const BOOST_DAYS = 7;

export default function BoostJobModal({ job, onClose, onSuccess }) {
  const { user } = useAuth();
  const tokenMode = user?.billing_mode === "tokens_v1";
  const [method, setMethod] = useState(null); // "stripe" | "paypal"
  const [loading, setLoading] = useState(false);
  const [tokenCost, setTokenCost] = useState(10);
  const tokenBoostRequestKey = useRef(null);

  useEffect(() => {
    if (!tokenMode) return;
    axios.get(`${API}/billing/catalog`).then(res =>
      setTokenCost(Number(res.data?.catalog?.token_costs?.contractor_job_boost ?? 10))
    ).catch(() => {});
  }, [tokenMode]);

  const handleTokenBoost = async () => {
    setLoading(true);
    try {
      if (!tokenBoostRequestKey.current) {
        const requestId = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
        tokenBoostRequestKey.current = `jobboost:${job.id}:${requestId}`;
      }
      const res = await axios.post(`${API}/billing/features/jobs/${job.id}/boost`, {}, {
        headers: { "Idempotency-Key": tokenBoostRequestKey.current }
      });
      tokenBoostRequestKey.current = null;
      toast.success(`Job boosted for ${BOOST_DAYS} days. ${res.data.tokens_charged || tokenCost} tokens charged.`);
      onSuccess?.();
    } catch (err) {
      const detail = err?.response?.data?.detail || "";
      if (err?.response && !(err.response.status === 409 && String(detail).includes("already processing"))) {
        tokenBoostRequestKey.current = null;
      }
      toast.error(detail || "Could not boost job with tokens");
    } finally {
      setLoading(false);
    }
  };

  const handleStripe = async () => {
    setLoading(true);
    try {
      const res = await axios.post(`${API}/payments/boost/stripe/${job.id}`, {
        origin_url: window.location.origin,
      });
      window.location.href = res.data.url;
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Stripe checkout failed");
      setLoading(false);
    }
  };

  const handlePayPal = async () => {
    setLoading(true);
    try {
      const res = await axios.post(`${API}/payments/boost/paypal/${job.id}`, {
        origin_url: window.location.origin,
      });
      window.open(res.data.approve_url, "_blank");
      toast.success("PayPal opened — complete payment then return here.");
      setLoading(false);
      onClose();
    } catch (err) {
      toast.error(err?.response?.data?.detail || "PayPal checkout failed");
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div className="bg-white dark:bg-slate-900 rounded-2xl p-6 w-full max-w-md shadow-2xl" onClick={e => e.stopPropagation()} data-testid="boost-modal">
        {/* Header */}
        <div className="flex items-start justify-between mb-5">
          <div className="flex items-center gap-2">
            <div className="w-10 h-10 bg-gradient-to-br from-yellow-400 to-orange-500 rounded-xl flex items-center justify-center">
              <Zap className="w-5 h-5 text-white" />
            </div>
            <div>
              <h2 className="font-extrabold text-[#050A30] dark:text-white text-lg" style={{ fontFamily: "Manrope, sans-serif" }}>Boost This Job</h2>
              <p className="text-xs text-slate-400">{tokenMode ? "Token wallet payment" : "One-time payment — no subscription required"}</p>
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-lg">
            <X className="w-4 h-4 text-slate-400" />
          </button>
        </div>

        {/* Job info */}
        <div className="bg-slate-50 dark:bg-slate-800 rounded-xl p-4 mb-5">
          <p className="font-semibold text-[#050A30] dark:text-white text-sm">{job.title}</p>
          <p className="text-xs text-slate-400 mt-0.5">{job.trade} · ${job.pay_rate}/hr</p>
        </div>

        {/* Benefits */}
        <div className="space-y-2 mb-5">
          {[
            "Job appears at the TOP of all crew feeds",
            `Push notification sent to all matching crew`,
            `Featured badge visible for ${BOOST_DAYS} days`,
            "3x more visibility guaranteed",
          ].map(b => (
            <div key={b} className="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300">
              <CheckCircle className="w-4 h-4 text-green-500 flex-shrink-0" />
              {b}
            </div>
          ))}
        </div>

        {/* Price */}
        <div className="bg-gradient-to-r from-blue-50 to-indigo-50 dark:from-blue-950/30 dark:to-indigo-950/30 border border-blue-100 dark:border-blue-900 rounded-xl p-4 mb-5 flex items-center justify-between">
          <div>
            <p className="text-xs text-slate-500 font-semibold uppercase tracking-wide">Boost Price</p>
            <p className="text-2xl font-extrabold text-[#0000FF]" style={{ fontFamily: "Manrope, sans-serif" }}>
              {tokenMode ? `${tokenCost} tokens` : `${BOOST_PRICE}`}
            </p>
            <p className="text-xs text-slate-400">One-time · {BOOST_DAYS} days featured</p>
          </div>
          <Zap className="w-8 h-8 text-yellow-500" />
        </div>

        {tokenMode ? (
          <button onClick={handleTokenBoost} disabled={loading}
            className="w-full flex items-center justify-center gap-2 py-3 px-4 rounded-xl bg-[#0000FF] text-white font-bold hover:bg-blue-700 disabled:opacity-60"
            data-testid="boost-token-btn">
            {loading ? <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-white" /> : <Zap className="w-5 h-5" />}
            Boost for {tokenCost} tokens
          </button>
        ) : null}

        {/* Legacy checkout remains for grandfathered accounts. */}
        {!tokenMode && !method ? (
          <div className="space-y-3">
            <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Choose Payment Method</p>
            <button onClick={() => setMethod("stripe")}
              className="w-full flex items-center justify-center gap-3 py-3 px-4 rounded-xl bg-[#0000FF] text-white font-bold hover:bg-blue-700 transition-colors"
              data-testid="boost-stripe-btn">
              <CreditCard className="w-5 h-5" />
              Pay with Card (Stripe)
            </button>
            <button onClick={() => setMethod("paypal")}
              className="w-full flex items-center justify-center gap-3 py-3 px-4 rounded-xl bg-[#003087] text-white font-bold hover:bg-blue-900 transition-colors"
              data-testid="boost-paypal-btn">
              <DollarSign className="w-5 h-5" />
              Pay with PayPal
            </button>
          </div>
        ) : !tokenMode && method ? (
          <div className="space-y-3">
            <div className="flex items-center gap-2 mb-1">
              <button onClick={() => setMethod(null)} className="text-xs text-slate-400 hover:text-slate-600">← Back</button>
              <span className="text-sm font-semibold text-[#050A30] dark:text-white capitalize">{method} Checkout</span>
            </div>
            <button
              onClick={method === "stripe" ? handleStripe : handlePayPal}
              disabled={loading}
              className="w-full flex items-center justify-center gap-2 py-3 px-4 rounded-xl bg-[#0000FF] text-white font-bold hover:bg-blue-700 disabled:opacity-60 transition-colors"
              data-testid="boost-confirm-btn"
            >
              {loading ? (
                <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-white" />
              ) : (
                <>
                  <ExternalLink className="w-4 h-4" />
                  Proceed to {method === "stripe" ? "Stripe" : "PayPal"} — ${BOOST_PRICE}
                </>
              )}
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
