import React, { useState, useEffect, useCallback, useRef } from "react";
import { useAuth } from "../contexts/AuthContext";
import { useWebSocket } from "../contexts/WebSocketContext";
import { useSearchParams } from "react-router-dom";
import Navbar from "../components/Navbar";
import MapComponent from "../components/MapComponent";
import JobCard from "../components/JobCard";
import DisputeModal from "../components/DisputeModal";
import DisputeCenter from "../components/DisputeCenter";
import { toast } from "sonner";
import axios from "axios";
import {
  Search, Plus, Zap, Users, Briefcase, Star, X,
  AlertTriangle, PauseCircle, XCircle, PlayCircle,
  Navigation, Send, CheckCircle, Shield, Loader2
} from "lucide-react";
import BoostJobModal from "../components/BoostJobModal";
import OfferCrewModal from "../components/OfferCrewModal";
import AddressAutocomplete from "../components/AddressAutocomplete";
import JobMediaInputs from "../components/JobMediaInputs";
import { US_STATES } from "../lib/usStates";
import SafeImage from "../components/SafeImage";
import { resolveImageUrl } from "../lib/media";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const TRADES = ["Carpentry", "Electrical", "Plumbing", "Painting", "Landscaping", "Masonry", "HVAC", "Roofing", "Drywall", "General Labor", "Demolition", "Concrete"];

function RatingModal({ job, onClose, onSubmit }) {
  const [ratings, setRatings] = useState({});
  const [reviews, setReviews] = useState({});
  return (
    <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
      <div className="card max-w-md w-full p-6 relative">
        <button onClick={onClose} className="absolute top-4 right-4 text-slate-400"><X className="w-5 h-5" /></button>
        <h2 className="font-extrabold text-[#050A30] dark:text-white text-xl mb-4" style={{ fontFamily: "Manrope, sans-serif" }}>Rate Workers</h2>
        {(job.crew_accepted || []).filter(crewId => !(job.rated_crew || []).includes(crewId)).map(crewId => (
          <div key={crewId} className="mb-4 p-3 bg-slate-50 dark:bg-slate-800 rounded-lg">
            <p className="text-sm font-semibold mb-2">Worker ID: {crewId.slice(0, 8)}...</p>
            <div className="flex gap-1 mb-2">
              {[1,2,3,4,5].map(s => (
                <button key={s} onClick={() => setRatings(r => ({ ...r, [crewId]: s }))}
                  className={`text-2xl transition-colors ${(ratings[crewId] || 0) >= s ? "text-amber-400" : "text-slate-300"}`}>★</button>
              ))}
            </div>
            <textarea placeholder="Write a review..." value={reviews[crewId] || ""}
              onChange={e => setReviews(r => ({ ...r, [crewId]: e.target.value }))}
              className="w-full border border-slate-200 dark:border-slate-600 rounded-lg p-2 text-sm dark:bg-slate-700 dark:text-white" rows={2} />
          </div>
        ))}
        <button onClick={() => onSubmit(job, ratings, reviews)}
          className="w-full bg-[#0000FF] text-white py-3 rounded-xl font-bold hover:bg-blue-700" data-testid="submit-ratings-btn">
          Submit Ratings
        </button>
      </div>
    </div>
  );
}

export default function ContractorDashboard() {
  const { user, setUser } = useAuth();
  const { addListener, connected } = useWebSocket();
  const [searchParams, setSearchParams] = useSearchParams();
  const [jobs, setJobs] = useState([]);
  const [crew, setCrew] = useState([]);
  const [crewSearch, setCrewSearch] = useState({ name: "", trade: "", skill: "", transportation: "", min_rating: "", state: "" });
  const [crewTradeOptions, setCrewTradeOptions] = useState(TRADES);
  const [showJobForm, setShowJobForm] = useState(false);
  const [ratingJob, setRatingJob] = useState(null);
  const [disputeJob, setDisputeJob] = useState(null);
  const [offerCrew, setOfferCrew] = useState(null);
  const [loading, setLoading] = useState(true);
  // Start from the saved business location; browser geolocation refines it if allowed
  const [userLocation, setUserLocation] = useState(() =>
    user?.location?.lat != null && user?.location?.lng != null ? { lat: user.location.lat, lng: user.location.lng } : null);
  const [flyToCoords, setFlyToCoords] = useState(null);
  const [selectedState, setSelectedState] = useState("");
  const [boostJob, setBoostJob] = useState(null);
  const [verifiedFee, setVerifiedFee] = useState(39.99);
  const [verifiedTokenCost, setVerifiedTokenCost] = useState({ standard: 100, pro: 80 });
  const [emergencyTokenCost, setEmergencyTokenCost] = useState(20);
  const jobSubmitIdempotency = useRef(null);
  const [verifiedLoading, setVerifiedLoading] = useState(false);
  const [pollingVerified, setPollingVerified] = useState(false);
  const [emergencyFee, setEmergencyFee] = useState(2.99);
  const [payingJobId, setPayingJobId] = useState(null);
  const [pollingEmergency, setPollingEmergency] = useState(false);
  const [postingJob, setPostingJob] = useState(false);
  const handledBoostReturn = useRef(null);
  const [jobForm, setJobForm] = useState({
    title: "", description: "", trade: "", crew_needed: 1,
    start_time: "", end_time: "", pay_rate: "", address: "", is_emergency: false, lat: null, lng: null, city: "", state: "", images: []
  });

  const fetchJobs = useCallback(async () => {
    try {
      const res = await axios.get(`${API}/jobs/`);
      setJobs(res.data);
    } catch (e) { console.error(e); }
  }, []);

  const fetchCrew = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (crewSearch.name) params.append("name", crewSearch.name);
      if (crewSearch.trade) params.append("trade", crewSearch.trade);
      if (crewSearch.skill) params.append("skill", crewSearch.skill);
      if (crewSearch.transportation) params.append("transportation", crewSearch.transportation);
      if (crewSearch.min_rating) params.append("min_rating", crewSearch.min_rating);
      if (crewSearch.state) params.append("state", crewSearch.state);
      const res = await axios.get(`${API}/users/crew?${params}`);
      setCrew(res.data);
    } catch (e) { console.error(e); }
  }, [crewSearch]);

  useEffect(() => {
    axios.get(`${API}/public/trades`).then(res => {
      const names = (res.data || []).filter(t => t?.name && t.is_active !== false).map(t => t.name);
      if (names.length) setCrewTradeOptions(names);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    const init = async () => {
      setLoading(true);
      await Promise.all([fetchJobs(), fetchCrew()]);
      setLoading(false);
    };
    init();
    // Auto get location
    navigator.geolocation?.getCurrentPosition(pos => {
      setUserLocation({ lat: pos.coords.latitude, lng: pos.coords.longitude });
    });
    // Fetch verified contractor fee
    axios.get(`${API}/payments/verified-contractor/fee`).then(r => setVerifiedFee(r.data.fee)).catch(() => {});
    axios.get(`${API}/payments/emergency/fee`).then(r => setEmergencyFee(r.data.fee)).catch(() => {});
    axios.get(`${API}/billing/catalog`).then(r => {
      const costs = r.data?.catalog?.token_costs || {};
      setVerifiedTokenCost({ standard: Number(costs.verified_contractor_renewal ?? 100), pro: Number(costs.pro_verified_contractor_renewal ?? 80) });
      setEmergencyTokenCost(Number(costs.contractor_post_job ?? 10) + Number(costs.emergency_job_surcharge ?? 10));
    }).catch(() => {});
  }, [fetchJobs, fetchCrew]);

  // Poll verified contractor payment status when redirected back
  useEffect(() => {
    const sessionId = searchParams.get("verified_session_id");
    if (!sessionId) return;
    setPollingVerified(true);
    let attempts = 0;
    const poll = async () => {
      try {
        const res = await axios.get(`${API}/payments/verified-contractor/status/${sessionId}`);
        if (res.data.payment_status === "paid") {
          toast.success("You are now a Verified Contractor!");
          if (setUser) setUser(u => ({ ...u, is_verified_contractor: true }));
          setPollingVerified(false);
          setSearchParams({});
          return;
        }
      } catch {}
      attempts++;
      if (attempts < 6) setTimeout(poll, 2000);
      else { setPollingVerified(false); toast.error("Payment status unknown. Refresh and check your profile."); }
    };
    poll();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Job boost: confirm when returning from Stripe (boost_session_id) or PayPal (boost_paypal + token)
  useEffect(() => {
    const stripeSession = searchParams.get("boost_session_id");
    const paypalOrder = searchParams.get("boost_paypal") ? searchParams.get("token") : null;
    const jobId = searchParams.get("job_id");
    if (!stripeSession && !paypalOrder) return;
    // Handle each return exactly once (StrictMode runs effects twice in development).
    const key = stripeSession || paypalOrder;
    if (handledBoostReturn.current === key) return;
    handledBoostReturn.current = key;
    let attempts = 0;
    let stopped = false;
    const done = (ok, msg) => {
      stopped = true;
      setSearchParams({});
      fetchJobs();
      ok ? toast.success(msg) : toast.error(msg);
    };
    const poll = async () => {
      if (stopped) return;
      try {
        if (paypalOrder) {
          const res = await axios.post(`${API}/payments/boost/paypal/capture/${paypalOrder}?job_id=${jobId || ""}`);
          if (res.data.status === "COMPLETED") return done(true, "Payment received! Your job is boosted for 7 days.");
          return done(false, "PayPal payment was not completed. Your job was not boosted.");
        }
        const res = await axios.get(`${API}/payments/boost/stripe/status/${stripeSession}`);
        if (res.data.already_processed || res.data.payment_status === "paid")
          return done(true, "Payment received! Your job is boosted for 7 days.");
        if (res.data.status === "expired") return done(false, "Checkout expired. Your job was not boosted.");
        if (res.data.payment_status === "verification_failed")
          return done(false, "We couldn't verify this payment. Please contact support - your job was not boosted.");
      } catch { /* retry below */ }
      attempts++;
      if (attempts < 8) setTimeout(poll, 2000);
      else done(false, "Payment status unknown. Refresh in a moment to check your boost.");
    };
    poll();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Emergency Job Start payment: confirm when returning from Stripe checkout
  useEffect(() => {
    const sessionId = searchParams.get("emergency_session");
    const cancelled = searchParams.get("emergency_cancelled");
    if (cancelled) {
      toast.info("Payment cancelled. Your emergency job is saved - pay anytime from My Jobs to publish it.");
      setSearchParams({});
      return;
    }
    if (!sessionId) return;
    setPollingEmergency(true);
    let attempts = 0;
    const poll = async () => {
      try {
        const res = await axios.get(`${API}/payments/emergency/status/${sessionId}`);
        if (res.data.published) {
          toast.success("Payment received! Your emergency job is live and crew are being alerted.");
          setPollingEmergency(false);
          setSearchParams({});
          fetchJobs();
          return;
        }
        if (res.data.status === "expired") {
          setPollingEmergency(false);
          setSearchParams({});
          toast.error("Checkout expired. Pay again from My Jobs to publish your emergency job.");
          return;
        }
      } catch { /* retry below */ }
      attempts++;
      if (attempts < 8) setTimeout(poll, 2000);
      else { setPollingEmergency(false); setSearchParams({}); fetchJobs(); toast.error("Payment status unknown. Refresh in a moment to check your job."); }
    };
    poll();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const startEmergencyCheckout = async (jobId) => {
    setPayingJobId(jobId);
    try {
      const res = await axios.post(`${API}/payments/emergency/stripe/${jobId}`, { origin_url: window.location.origin });
      window.location.href = res.data.url;
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not start emergency checkout");
      setPayingJobId(null);
    }
  };

  const deletePendingJob = async (jobId) => {
    if (!window.confirm("Delete this unpaid emergency job?")) return;
    try {
      await axios.delete(`${API}/jobs/${jobId}`);
      setJobs(prev => prev.filter(j => j.id !== jobId));
      toast.success("Unpaid job deleted");
    } catch (e) { toast.error(e?.response?.data?.detail || "Failed to delete job"); }
  };

  useEffect(() => {
    const remove = addListener(msg => {
      if (msg.type === "job_accepted") {
        toast.success(`Worker accepted your job! (${msg.crew_count}/${msg.crew_needed} filled)`);
        fetchJobs();
      }
      if (msg.type === "job_completed") {
        toast.info(`Job "${msg.job_title}" has been marked complete by crew. Please verify.`);
        fetchJobs();
      }
    });
    return remove;
  }, [addListener, fetchJobs]);

  const startVerifiedCheckout = async () => {
    setVerifiedLoading(true);
    try {
      if (user?.billing_mode === "tokens_v1") {
        const key = "verification:" + user.id + ":" + (user.verified_expires_at || "initial");
        const res = await axios.post(`${API}/billing/features/verification/renew`, {}, {
          headers: { "Idempotency-Key": key }
        });
        if (setUser) setUser(u => ({ ...u, is_verified_contractor: true, verified_expires_at: res.data.verified_expires_at }));
        toast.success(`Verification renewed for one year. ${res.data.tokens_charged} tokens charged.`);
        setVerifiedLoading(false);
        return;
      }
      const res = await axios.post(`${API}/payments/verified-contractor/create-session`, { origin_url: window.location.origin });
      window.location.href = res.data.url;
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not start checkout");
      setVerifiedLoading(false);
    }
  };

  const createJob = async (e) => {
    e?.preventDefault();
    if (postingJob) return;
    if (jobForm.end_time && jobForm.start_time && new Date(jobForm.end_time) <= new Date(jobForm.start_time)) {
      return toast.error("End time must be after start time");
    }
    const confirmMsg = jobForm.is_emergency
      ? (user?.billing_mode === "tokens_v1"
          ? `Post "${jobForm.title}" as an EMERGENCY job for ${emergencyTokenCost} tokens? Crew are alerted immediately.`
          : `Post "${jobForm.title}" as an EMERGENCY job? You'll pay a one-time $${emergencyFee.toFixed(2)} Emergency Job Start fee, then crew are alerted immediately.`)
      : `Publish "${jobForm.title}" now? Crew will be notified immediately.`;
    if (!window.confirm(confirmMsg)) return;
    setPostingJob(true);
    try {
      if (!jobSubmitIdempotency.current) {
        const randomId = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
        jobSubmitIdempotency.current = `jobpost:${user?.id || "user"}:${randomId}`;
      }
      const res = await axios.post(`${API}/jobs/`, {
        ...jobForm,
        crew_needed: Number(jobForm.crew_needed),
        pay_rate: Number(jobForm.pay_rate)
      }, { headers: { "Idempotency-Key": jobSubmitIdempotency.current } });
      const newJob = res.data;
      jobSubmitIdempotency.current = null;
      // Show the new job right away, then sync with the server
      setJobs(prev => [newJob, ...prev.filter(j => j.id !== newJob.id)]);
      setShowJobForm(false);
      setJobForm({ title: "", description: "", trade: "", crew_needed: 1, start_time: "", end_time: "", pay_rate: "", address: "", is_emergency: false, lat: null, lng: null, city: "", state: "", images: [] });
      if (newJob.requires_payment) {
        toast.info("Job saved. Redirecting to secure checkout for the Emergency Job Start fee...");
        await startEmergencyCheckout(newJob.id);
      } else {
        toast.success("Job posted! Workers will be notified instantly.");
        fetchJobs();
      }
    } catch (e) {
      const detail = e?.response?.data?.detail || "";
      if (e?.response && !(e.response.status === 409 && String(detail).includes("already processing"))) {
        jobSubmitIdempotency.current = null;
      }
      toast.error(detail || "Failed to post job");
    } finally {
      setPostingJob(false);
    }
  };

  const startJob = async (jobId) => {
    try { await axios.post(`${API}/jobs/${jobId}/start`); toast.success("Job started!"); fetchJobs(); }
    catch (e) { toast.error(e?.response?.data?.detail || "Failed"); }
  };

  const verifyJob = async (jobId) => {
    try { await axios.post(`${API}/jobs/${jobId}/verify`); toast.success("Job verified and completed!"); fetchJobs(); }
    catch (e) { toast.error(e?.response?.data?.detail || "Failed"); }
  };

  const cancelJob = async (jobId, title) => {
    const reason = window.prompt(`Reason for cancelling "${title}"? (optional)`);
    if (reason === null) return;
    try {
      const res = await axios.post(`${API}/jobs/${jobId}/cancel`, { reason: reason || "" });
      toast.success(`Job cancelled. ${res.data.crew_notified} crew member(s) notified.`);
      fetchJobs();
    } catch (e) { toast.error(e?.response?.data?.detail || "Failed to cancel job"); }
  };

  const archiveJob = async (jobId) => {
    if (!window.confirm("Archive this cancelled job?")) return;
    try {
      await axios.post(`${API}/jobs/${jobId}/archive`);
      setJobs(prev => prev.filter(job => job.id !== jobId));
      toast.success("Job archived");
    } catch (e) { toast.error(e?.response?.data?.detail || "Failed to archive job"); }
  };

  const suspendJob = async (jobId) => {
    try {
      const res = await axios.post(`${API}/jobs/${jobId}/suspend`);
      toast.success(`Job paused. ${res.data.crew_notified} crew member(s) notified.`);
      fetchJobs();
    } catch (e) { toast.error(e?.response?.data?.detail || "Failed to suspend job"); }
  };

  const resumeJob = async (jobId) => {
    try { await axios.post(`${API}/jobs/${jobId}/resume`); toast.success("Job resumed!"); fetchJobs(); }
    catch (e) { toast.error(e?.response?.data?.detail || "Failed to resume job"); }
  };

  const submitRatings = async (job, ratings, reviews) => {
    try {
      for (const [crewId, stars] of Object.entries(ratings)) {
        if (stars > 0) {
          await axios.post(`${API}/jobs/${job.id}/rate`, { rated_id: crewId, job_id: job.id, stars, review: reviews[crewId] || "" });
        }
      }
      toast.success("Ratings submitted!");
    } catch (error) {
      toast.error(error?.response?.data?.detail || "Some ratings could not be submitted");
    } finally {
      setRatingJob(null);
      fetchJobs();
    }
  };

  const saveFavorite = async (crewId) => {
    try { await axios.post(`${API}/users/favorites/${crewId}`); toast.success("Added to favorites"); }
    catch { toast.error("Failed"); }
  };

  const handleStateChange = (e) => {
    const abbr = e.target.value;
    setSelectedState(abbr);
    if (!abbr) return;
    const st = US_STATES.find(s => s.abbr === abbr);
    if (st) setFlyToCoords([st.lng, st.lat]);
  };

  const updateForm = (k, v) => setJobForm(f => ({ ...f, [k]: v }));

  // Only show contractor's own jobs on map
  const myMapJobs = jobs.filter(j => ["open", "fulfilled", "in_progress"].includes(j.status));

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-[#020617]" style={{ fontFamily: "Inter, sans-serif" }}>
      <Navbar />
      {boostJob && <BoostJobModal job={boostJob} onClose={() => setBoostJob(null)} onSuccess={() => { setBoostJob(null); fetchJobs(); }} />}
      {ratingJob && <RatingModal job={ratingJob} onClose={() => setRatingJob(null)} onSubmit={submitRatings} />}
      {disputeJob && <DisputeModal job={disputeJob.job} initialType={disputeJob.initialType} currentUser={user}
        onClose={() => setDisputeJob(null)} onSubmitted={() => { setDisputeJob(null); fetchJobs(); }} />}
      {offerCrew && <OfferCrewModal crew={offerCrew} jobs={jobs} onClose={() => setOfferCrew(null)} />}

      <div className="max-w-[1400px] mx-auto px-4 py-4">
        <DisputeCenter jobs={jobs} />

        {pollingEmergency && (
          <div className="mb-4 bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-800 rounded-xl p-4 flex items-center gap-3" data-testid="polling-emergency-banner">
            <Loader2 className="w-5 h-5 text-red-600 dark:text-red-400 animate-spin flex-shrink-0" />
            <div>
              <p className="font-bold text-[#050A30] dark:text-white text-sm">Confirming your emergency payment...</p>
              <p className="text-slate-500 dark:text-slate-400 text-xs">Your job goes live as soon as the payment is verified.</p>
            </div>
          </div>
        )}

        {/* Verified Contractor Banner */}
        {pollingVerified && (
          <div className="mb-4 bg-blue-50 dark:bg-blue-950 border border-[#0000FF]/30 rounded-xl p-4 flex items-center gap-3" data-testid="polling-verified-banner">
            <Loader2 className="w-5 h-5 text-[#0000FF] animate-spin flex-shrink-0" />
            <div>
              <p className="font-bold text-[#050A30] dark:text-white text-sm">Confirming your payment...</p>
              <p className="text-slate-500 text-xs">Please wait while we verify your Verified Contractor payment.</p>
            </div>
          </div>
        )}
        {!pollingVerified && user?.is_verified_contractor && (
          <div className="mb-4 bg-green-50 dark:bg-green-950 border border-green-300 dark:border-green-700 rounded-xl p-4 flex items-center gap-3" data-testid="verified-badge-banner">
            <CheckCircle className="w-6 h-6 text-green-500 flex-shrink-0" />
            <div>
              <p className="font-bold text-green-700 dark:text-green-300">You are a Verified Contractor!</p>
              <p className="text-green-600 dark:text-green-400 text-xs">Your profile appears on the Verified Contractors page with a verified badge.</p>
            </div>
            <a href="/verified-contractors" target="_blank" rel="noreferrer"
              className="ml-auto text-xs text-[#0000FF] font-bold hover:underline flex-shrink-0"
              data-testid="view-verified-page-link">View Page</a>
          </div>
        )}
        {!pollingVerified && !user?.is_verified_contractor && (
          <div className="mb-4 bg-gradient-to-r from-[#050A30] to-[#0000FF] rounded-xl p-4 flex flex-col sm:flex-row items-start sm:items-center gap-4" data-testid="get-verified-banner">
            <div className="flex items-center gap-3 flex-1">
              <Shield className="w-8 h-8 text-white flex-shrink-0" />
              <div>
                <p className="font-extrabold text-white" style={{ fontFamily: "Manrope, sans-serif" }}>Get Verified — Stand Out from the Crowd</p>
                <p className="text-blue-200 text-xs">Pay a one-time fee to appear on the Verified Contractors page visible to homeowners and property managers.</p>
              </div>
            </div>
            <button onClick={startVerifiedCheckout} disabled={verifiedLoading}
              className="flex items-center gap-2 bg-white text-[#0000FF] px-5 py-2.5 rounded-xl font-extrabold text-sm hover:bg-blue-50 transition-colors disabled:opacity-70 flex-shrink-0"
              data-testid="get-verified-btn">
              {verifiedLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle className="w-4 h-4" />}
              {verifiedLoading ? "Loading..." : user?.billing_mode === "tokens_v1" ? `Get Verified — ${(user?.billing_plan === "pro_contractor" || user?.billing_subscription_status === "trial") ? verifiedTokenCost.pro : verifiedTokenCost.standard} tokens` : `Get Verified — ${verifiedFee}`}
            </button>
          </div>
        )}

        <div className="flex items-center justify-between mb-4">
          <div>
            <h1 className="text-2xl font-extrabold text-[#050A30] dark:text-white" style={{ fontFamily: "Manrope, sans-serif" }}>
              {user?.company_name || user?.name}
            </h1>
            <p className="text-sm text-slate-500 flex items-center gap-1">
              <span className={`w-2 h-2 rounded-full ${connected ? "bg-emerald-500" : "bg-red-400"}`} />
              {connected ? "Live updates active" : "Connecting..."}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {/* State selector */}
            <select value={selectedState} onChange={handleStateChange}
              className="border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white"
              data-testid="contractor-state-select">
              <option value="">All States</option>
              {US_STATES.map(s => <option key={s.abbr} value={s.abbr}>{s.name}</option>)}
            </select>
            <button onClick={() => setShowJobForm(true)}
              className="flex items-center gap-2 bg-[#0000FF] text-white px-4 py-2.5 rounded-xl font-bold hover:bg-blue-700 transition-colors"
              data-testid="post-job-btn">
              <Plus className="w-4 h-4" /> Post Job
            </button>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
          {/* LEFT SIDEBAR */}
          <div className="lg:col-span-3 space-y-3">
            <div className="card p-4">
              <h3 className="font-bold text-[#050A30] dark:text-white text-sm mb-3" style={{ fontFamily: "Manrope, sans-serif" }}>Search Crew</h3>
              <div className="space-y-2">
                <div className="relative">
                  <Search className="w-4 h-4 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2" />
                  <input type="text" placeholder="Name..." value={crewSearch.name}
                    onChange={e => setCrewSearch(s => ({ ...s, name: e.target.value }))}
                    className="w-full pl-9 pr-3 py-2 border border-slate-200 dark:border-slate-600 rounded-lg text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white"
                    data-testid="crew-search-name" />
                </div>
                <select value={crewSearch.trade} onChange={e => setCrewSearch(s => ({ ...s, trade: e.target.value }))}
                  className="w-full border border-slate-200 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white"
                  data-testid="crew-search-trade">
                  <option value="">All Trades</option>
                  {crewTradeOptions.map(t => <option key={t} value={t.toLowerCase()}>{t}</option>)}
                </select>
                <select value={crewSearch.skill} onChange={e => setCrewSearch(s => ({ ...s, skill: e.target.value }))}
                  className="w-full border border-slate-200 dark:border-slate-600 rounded-lg px-3 py-2 text-sm dark:bg-slate-800 dark:text-white"
                  data-testid="crew-search-skill">
                  <option value="">Any Skill</option>
                  {crewTradeOptions.map(t => <option key={t} value={t}>{t}</option>)}
                </select>
                <select value={crewSearch.transportation} onChange={e => setCrewSearch(s => ({ ...s, transportation: e.target.value }))}
                  className="w-full border border-slate-200 dark:border-slate-600 rounded-lg px-3 py-2 text-sm dark:bg-slate-800 dark:text-white"
                  data-testid="crew-search-transportation">
                  <option value="">Any Transportation</option>
                  <option value="bike">Bike</option><option value="car">Car</option><option value="suv_van">SUV / Van</option>
                  <option value="truck">Truck</option><option value="bus">Bus</option><option value="rideshare">Rideshare</option><option value="other">Other</option>
                </select>
                <select value={crewSearch.min_rating} onChange={e => setCrewSearch(s => ({ ...s, min_rating: e.target.value }))}
                  className="w-full border border-slate-200 dark:border-slate-600 rounded-lg px-3 py-2 text-sm dark:bg-slate-800 dark:text-white"
                  data-testid="crew-search-rating">
                  <option value="">Any Rating</option><option value="3">3+ stars</option><option value="4">4+ stars</option><option value="4.5">4.5+ stars</option>
                </select>
                <select value={crewSearch.state} onChange={e => setCrewSearch(s => ({ ...s, state: e.target.value }))}
                  className="w-full border border-slate-200 dark:border-slate-600 rounded-lg px-3 py-2 text-sm dark:bg-slate-800 dark:text-white"
                  data-testid="crew-search-state">
                  <option value="">Any State</option>
                  {US_STATES.map(s => <option key={s.abbr} value={s.name}>{s.name}</option>)}
                </select>
                <button type="button" onClick={() => setCrewSearch({ name: "", trade: "", skill: "", transportation: "", min_rating: "", state: "" })}
                  className="text-xs font-semibold text-[#0000FF] hover:underline justify-self-start" data-testid="reset-crew-filters">Reset filters</button>
              </div>
            </div>

            <div className="card overflow-hidden">
              <div className="p-3 border-b border-slate-100 dark:border-slate-700">
                <h3 className="font-bold text-[#050A30] dark:text-white text-sm" style={{ fontFamily: "Manrope, sans-serif" }}>Available Crew ({crew.length})</h3>
              </div>
              <div className="max-h-96 overflow-y-auto">
                {crew.length === 0 ? (
                  <div className="p-6 text-center">
                    <Users className="w-8 h-8 text-slate-300 mx-auto mb-2" />
                    <p className="text-slate-400 text-sm">No crew found</p>
                  </div>
                ) : crew.map(member => (
                  <div key={member.id} className="flex items-center gap-3 p-3 border-b border-slate-50 dark:border-slate-800 hover:bg-slate-50 dark:hover:bg-slate-800"
                    data-testid={`crew-member-${member.id}`}>
                    <div className="w-9 h-9 rounded-full overflow-hidden bg-[#050A30] flex items-center justify-center text-white text-xs font-bold flex-shrink-0">
                      {member.profile_photo
                        ? <SafeImage src={resolveImageUrl(member.profile_photo)} alt="" className="w-full h-full object-cover" />
                        : member.name?.[0]?.toUpperCase()}
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-semibold text-[#050A30] dark:text-white truncate">{member.name}</p>
                      {member.search_featured && <span className="inline-flex mt-0.5 px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 text-[10px] font-bold" data-testid={`featured-crew-${member.id}`}>FEATURED</span>}
                      <p className="text-xs text-slate-500 capitalize">{member.trade || "General"}</p>
                    </div>
                    <div className="flex flex-col items-end gap-1">
                      <div className="flex items-center gap-0.5 text-xs text-amber-500">
                        <Star className="w-3 h-3 fill-current" />
                        <span>{member.rating?.toFixed(1) || "New"}</span>
                      </div>
                      <div className="flex gap-1">
                        <button onClick={() => saveFavorite(member.id)} className="text-slate-400 hover:text-amber-400" title="Favorite">
                          <Star className="w-3.5 h-3.5" />
                        </button>
                        <button onClick={() => setOfferCrew(member)}
                          className="text-[#0000FF] hover:text-blue-800 text-xs font-bold px-1.5 py-0.5 border border-[#0000FF] rounded"
                          data-testid={`offer-crew-${member.id}`}
                          title="Offer Job">
                          Offer
                        </button>
                        <a href={`/profile/${member.id}`} target="_blank" rel="noopener noreferrer"
                          className="inline-flex items-center px-2 py-1 text-xs font-semibold text-[#0000FF] bg-blue-50 border border-blue-200 rounded hover:bg-blue-100 transition-colors"
                          data-testid={`view-profile-crew-${member.id}`}
                          title="View Profile">
                          Profile
                        </a>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* CENTER - Map (contractor's jobs only) */}
          <div className="lg:col-span-6">
            <MapComponent
              jobs={myMapJobs}
              crew={crew}
              userLocation={userLocation}
              onCrewOffer={setOfferCrew}
              height="580px"
              flyToCoords={flyToCoords}
            />
          </div>

          {/* RIGHT SIDEBAR - Jobs */}
          <div className="lg:col-span-3 space-y-3">
            <div className="card p-4">
              <h3 className="font-bold text-[#050A30] dark:text-white text-sm mb-3" style={{ fontFamily: "Manrope, sans-serif" }}>My Jobs ({jobs.length})</h3>
              <div className="flex gap-2 mb-3">
                <div className="text-center flex-1 bg-emerald-50 dark:bg-emerald-950 rounded-lg p-2">
                  <div className="font-extrabold text-emerald-600">{jobs.filter(j => j.status === "open").length}</div>
                  <div className="text-xs text-slate-500">Open</div>
                </div>
                <div className="text-center flex-1 bg-blue-50 dark:bg-blue-950 rounded-lg p-2">
                  <div className="font-extrabold text-blue-600">{jobs.filter(j => j.status === "in_progress").length}</div>
                  <div className="text-xs text-slate-500">Active</div>
                </div>
                <div className="text-center flex-1 bg-gray-50 dark:bg-gray-900 rounded-lg p-2">
                  <div className="font-extrabold text-gray-600">{jobs.filter(j => j.status === "completed").length}</div>
                  <div className="text-xs text-slate-500">Done</div>
                </div>
              </div>
            </div>

            <div className="space-y-2 max-h-[460px] overflow-y-auto pr-1">
              {jobs.length === 0 ? (
                <div className="card p-6 text-center">
                  <Briefcase className="w-10 h-10 text-slate-300 mx-auto mb-3" />
                  <p className="text-slate-500 font-semibold text-sm">No jobs yet</p>
                  <button onClick={() => setShowJobForm(true)} className="mt-3 text-[#0000FF] text-sm font-semibold">Post your first job</button>
                </div>
              ) : jobs.map(job => (
                <div key={job.id}>
                  <JobCard job={job} onStart={startJob} onVerify={verifyJob} onRate={setRatingJob}
                    onReportDispute={(targetJob, initialType) => setDisputeJob({ job: targetJob, initialType })}
                    currentUser={user} />
                  {job.status === "pending_payment" && (
                    <div className="flex gap-2 mt-1 px-1" data-testid={`pending-payment-actions-${job.id}`}>
                      <button onClick={() => startEmergencyCheckout(job.id)} disabled={payingJobId === job.id}
                        className="flex-[2] flex items-center justify-center gap-1 py-1.5 text-xs font-bold text-white bg-red-600 hover:bg-red-700 rounded-lg disabled:opacity-60"
                        data-testid={`pay-emergency-${job.id}`}>
                        {payingJobId === job.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <AlertTriangle className="w-3.5 h-3.5" />}
                        Pay ${emergencyFee.toFixed(2)} to publish
                      </button>
                      <button onClick={() => deletePendingJob(job.id)}
                        className="flex-1 flex items-center justify-center gap-1 py-1.5 text-xs font-semibold text-slate-600 dark:text-slate-300 bg-slate-100 dark:bg-slate-800 rounded-lg border border-slate-200 dark:border-slate-700 hover:bg-slate-200 dark:hover:bg-slate-700"
                        data-testid={`delete-pending-${job.id}`}>
                        <XCircle className="w-3.5 h-3.5" /> Delete
                      </button>
                    </div>
                  )}
                  {job.status === "cancelled" && (
                    <div className="mt-1 px-1">
                      <button onClick={() => archiveJob(job.id)}
                        className="inline-flex items-center gap-1 px-3 py-1.5 text-xs font-semibold text-slate-700 bg-slate-100 rounded-lg border border-slate-200 hover:bg-slate-200"
                        data-testid={`archive-job-${job.id}`}>
                        Archive
                      </button>
                    </div>
                  )}
                  {!["completed", "cancelled", "completed_pending_review", "pending_payment"].includes(job.status) && (
                    <div className="flex gap-2 mt-1 px-1 flex-wrap">
                      {!job.is_featured && (
                        <button onClick={() => setBoostJob(job)}
                          className="flex items-center justify-center gap-1 py-1.5 px-3 text-xs font-semibold text-yellow-700 bg-yellow-50 dark:bg-yellow-900/20 rounded-lg border border-yellow-200 hover:bg-yellow-100"
                          data-testid={`boost-job-${job.id}`}>
                          <Zap className="w-3.5 h-3.5" /> Boost
                        </button>
                      )}
                      {job.is_featured && (
                        <span className="flex items-center gap-1 py-1.5 px-3 text-xs font-semibold text-yellow-700 bg-yellow-50 rounded-lg border border-yellow-200">
                          <Zap className="w-3.5 h-3.5" /> Featured
                        </span>
                      )}
                      {job.status !== "suspended" ? (
                        <>
                          <button onClick={() => suspendJob(job.id)} className="flex-1 flex items-center justify-center gap-1 py-1.5 text-xs font-semibold text-amber-600 bg-amber-50 dark:bg-amber-900/20 rounded-lg border border-amber-200 hover:bg-amber-100" data-testid={`suspend-job-${job.id}`}>
                            <PauseCircle className="w-3.5 h-3.5" /> Pause
                          </button>
                          <button onClick={() => cancelJob(job.id, job.title)} className="flex-1 flex items-center justify-center gap-1 py-1.5 text-xs font-semibold text-red-600 bg-red-50 dark:bg-red-900/20 rounded-lg border border-red-200 hover:bg-red-100" data-testid={`cancel-job-${job.id}`}>
                            <XCircle className="w-3.5 h-3.5" /> Cancel
                          </button>
                        </>
                      ) : (
                        <button onClick={() => resumeJob(job.id)} className="flex-1 flex items-center justify-center gap-1 py-1.5 text-xs font-semibold text-green-600 bg-green-50 dark:bg-green-900/20 rounded-lg border border-green-200 hover:bg-green-100" data-testid={`resume-job-${job.id}`}>
                          <PlayCircle className="w-3.5 h-3.5" /> Resume
                        </button>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* Create Job Modal */}
      {showJobForm && (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4 overflow-y-auto">
          <div className="card max-w-lg w-full p-6 relative my-4">
            <button onClick={() => setShowJobForm(false)} className="absolute top-4 right-4 text-slate-400 hover:text-slate-600"><X className="w-5 h-5" /></button>
            <h2 className="font-extrabold text-[#050A30] dark:text-white text-xl mb-1" style={{ fontFamily: "Manrope, sans-serif" }}>Post a Job</h2>
            <p className="text-slate-500 text-sm mb-5">Workers will be notified in real-time</p>

            <form onSubmit={createJob} className="space-y-4">
              <div className="flex gap-2 mb-2">
                <button type="button" onClick={() => updateForm("is_emergency", false)}
                  className={`flex-1 py-2.5 rounded-lg font-bold text-sm border-2 transition-colors ${!jobForm.is_emergency ? "bg-emerald-600 text-white border-emerald-600" : "bg-white dark:bg-slate-800 text-slate-500 border-slate-200"}`}
                  data-testid="regular-job-btn">Regular Job</button>
                <button type="button" onClick={() => updateForm("is_emergency", true)}
                  className={`flex-1 py-2.5 rounded-lg font-bold text-sm border-2 flex items-center justify-center gap-1 transition-colors ${jobForm.is_emergency ? "bg-yellow-400 text-[#050A30] border-yellow-400" : "bg-white dark:bg-slate-800 text-slate-500 border-slate-200"}`}
                  data-testid="emergency-job-btn">
                  <AlertTriangle className="w-4 h-4" /> Emergency · ${emergencyFee.toFixed(2)}
                </button>
              </div>
              {jobForm.is_emergency && (
                <div className="flex items-start gap-2 rounded-lg border border-yellow-300 bg-yellow-50 dark:bg-yellow-900/20 dark:border-yellow-700 p-3 text-xs text-[#050A30] dark:text-yellow-100" data-testid="emergency-fee-note">
                  <AlertTriangle className="w-4 h-4 text-yellow-600 dark:text-yellow-400 flex-shrink-0 mt-0.5" />
                  <span>A one-time <strong>${emergencyFee.toFixed(2)} Emergency Job Start</strong> fee applies. Your job is saved first, then goes live with priority alerts to nearby crew as soon as payment is complete.</span>
                </div>
              )}

              <div>
                <label className="block text-sm font-semibold text-[#050A30] dark:text-white mb-1">Job Title *</label>
                <input type="text" required value={jobForm.title} onChange={e => updateForm("title", e.target.value)}
                  className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white"
                  placeholder="e.g. Framing Crew Needed" data-testid="job-title-input" />
              </div>

              <div>
                <label className="block text-sm font-semibold text-[#050A30] dark:text-white mb-1">Description *</label>
                <textarea required minLength={10} value={jobForm.description} onChange={e => updateForm("description", e.target.value)} rows={3}
                  className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white"
                  placeholder="Describe the work..." data-testid="job-desc-input" />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-semibold text-[#050A30] dark:text-white mb-1">Trade *</label>
                  <select required value={jobForm.trade} onChange={e => updateForm("trade", e.target.value)}
                    className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white"
                    data-testid="job-trade-select">
                    <option value="">Select trade</option>
                    {TRADES.map(t => <option key={t} value={t.toLowerCase()}>{t}</option>)}
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-semibold text-[#050A30] dark:text-white mb-1">Crew Needed *</label>
                  <input type="number" min="1" max="50" required value={jobForm.crew_needed} onChange={e => updateForm("crew_needed", e.target.value)}
                    className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white"
                    data-testid="job-crew-needed-input" />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-semibold text-[#050A30] dark:text-white mb-1">Start Date/Time *</label>
                  <input type="datetime-local" required value={jobForm.start_time} onChange={e => updateForm("start_time", e.target.value)}
                    className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white"
                    data-testid="job-start-time-input" />
                </div>
                <div>
                  <label className="block text-sm font-semibold text-[#050A30] dark:text-white mb-1">End Date/Time</label>
                  <input type="datetime-local" value={jobForm.end_time}
                    min={jobForm.start_time}
                    onChange={e => updateForm("end_time", e.target.value)}
                    className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white"
                    data-testid="job-end-time-input" />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-semibold text-[#050A30] dark:text-white mb-1">Pay Rate ($/hr) *</label>
                  <input type="number" min="1" step="0.50" required value={jobForm.pay_rate} onChange={e => updateForm("pay_rate", e.target.value)}
                    className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white"
                    placeholder="25.00" data-testid="job-pay-rate-input" />
                </div>
                <div>
                  <label className="block text-sm font-semibold text-[#050A30] dark:text-white mb-1">Job Location *</label>
                  <AddressAutocomplete required value={jobForm.address} testId="job-address-input" placeholder="123 Main St, Miami, FL"
                    onChange={v => setJobForm(f => ({ ...f, address: v, lat: null, lng: null }))}
                    onSelect={({ address, lat, lng, city, state }) => setJobForm(f => ({ ...f, address, lat, lng, city, state }))} />
                </div>
              </div>

              <JobMediaInputs
                images={jobForm.images}
                onImages={imgs => updateForm("images", imgs)}
                onTranscript={text => setJobForm(f => ({ ...f, description: f.description ? `${f.description} ${text}` : text }))} />

              <div className="flex gap-3">
                <button type="button" onClick={() => setShowJobForm(false)}
                  className="flex-1 border border-slate-300 dark:border-slate-600 text-slate-700 dark:text-slate-200 py-3 rounded-xl font-bold hover:bg-slate-50 dark:hover:bg-slate-800 transition-colors"
                  data-testid="cancel-job-btn">
                  Cancel
                </button>
                <button type="submit" disabled={postingJob}
                  className="flex-[2] bg-[#0000FF] text-white py-3 rounded-xl font-bold hover:bg-blue-700 transition-colors disabled:opacity-60"
                  data-testid="submit-job-btn">
                  {postingJob ? "Posting..." : jobForm.is_emergency ? `Pay $${emergencyFee.toFixed(2)} & Post` : "Post Job Now"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
