import React, { useState, useEffect, useCallback } from "react";
import { useAuth } from "../contexts/AuthContext";
import { useLanguage } from "../contexts/LanguageContext";
import { useWebSocket } from "../contexts/WebSocketContext";
import Navbar from "../components/Navbar";
import MapComponent, { haversine } from "../components/MapComponent";
import JobCard from "../components/JobCard";
import DisputeModal from "../components/DisputeModal";
import DisputeCenter from "../components/DisputeCenter";
import { toast } from "sonner";
import axios from "axios";
import {
  MapPin, List, Zap, Clock, RefreshCw, AlertCircle, X,
  ToggleLeft, ToggleRight, Truck, Navigation, Share2, Bell, BellOff, BellRing
} from "lucide-react";
import { US_STATES } from "../lib/usStates";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const VAPID_PUBLIC_KEY = process.env.REACT_APP_VAPID_PUBLIC_KEY || "";

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = window.atob(base64);
  return Uint8Array.from([...rawData].map(c => c.charCodeAt(0)));
}

const TRANSPORT_OPTIONS = [
  { value: "bike", label: "Bike" },
  { value: "car", label: "Car" },
  { value: "suv_van", label: "SUV/Van" },
  { value: "truck", label: "Truck" },
  { value: "bus", label: "Bus" },
  { value: "rideshare", label: "Rideshare" },
  { value: "other", label: "Other" },
];

const DISTANCE_OPTIONS = [5, 10, 20, 25, 50, 100];

// ─── Push Notification Subscription Widget ────────────────────────────────────
function PushNotifyWidget() {
  const [status, setStatus] = useState("idle"); // idle | requesting | subscribed | blocked | unsupported
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!("Notification" in window) || !("serviceWorker" in navigator)) {
      setStatus("unsupported"); return;
    }
    if (Notification.permission === "denied") { setStatus("blocked"); return; }
    // Check if already subscribed
    navigator.serviceWorker.ready.then(sw =>
      sw.pushManager.getSubscription().then(sub => {
        if (sub) setStatus("subscribed");
      })
    ).catch(() => {});
  }, []);

  const subscribe = async () => {
    if (!VAPID_PUBLIC_KEY) { toast.error("Push notifications not configured"); return; }
    setLoading(true);
    try {
      const perm = await Notification.requestPermission();
      if (perm === "denied") { setStatus("blocked"); toast.error("Notifications blocked in browser settings"); setLoading(false); return; }
      const sw = await navigator.serviceWorker.ready;
      const sub = await sw.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC_KEY) });
      await axios.post(`${API}/users/push/subscribe`, sub.toJSON());
      setStatus("subscribed");
      toast.success("Job alerts enabled! You'll be notified of new jobs.");
    } catch (e) {
      toast.error("Could not enable notifications: " + (e.message || String(e)));
    }
    setLoading(false);
  };

  const unsubscribe = async () => {
    setLoading(true);
    try {
      const sw = await navigator.serviceWorker.ready;
      const sub = await sw.pushManager.getSubscription();
      if (sub) await sub.unsubscribe();
      setStatus("idle");
      toast.success("Job alerts disabled");
    } catch {}
    setLoading(false);
  };

  if (status === "unsupported") return null;

  return (
    <div className={`card p-4 border-2 ${status === "subscribed" ? "border-green-400 dark:border-green-600" : "border-dashed border-slate-300 dark:border-slate-600"}`}
      data-testid="push-notify-widget">
      <div className="flex items-center gap-2 mb-2">
        {status === "subscribed"
          ? <BellRing className="w-4 h-4 text-green-500" />
          : status === "blocked"
          ? <BellOff className="w-4 h-4 text-red-400" />
          : <Bell className="w-4 h-4 text-[#0000FF]" />}
        <h3 className="font-bold text-[#050A30] dark:text-white text-sm" style={{ fontFamily: "Manrope, sans-serif" }}>
          {status === "subscribed" ? "Job Alerts On" : "Get Job Alerts"}
        </h3>
      </div>

      {status === "subscribed" ? (
        <>
          <p className="text-xs text-slate-500 mb-3">You'll receive a push notification when new jobs are posted near you.</p>
          <button onClick={unsubscribe} disabled={loading}
            className="w-full border border-slate-300 dark:border-slate-600 rounded-lg py-2 text-xs font-semibold text-slate-500 hover:text-red-500 hover:border-red-300 transition-colors disabled:opacity-50"
            data-testid="disable-push-btn">
            {loading ? "Updating..." : "Turn Off Alerts"}
          </button>
        </>
      ) : status === "blocked" ? (
        <p className="text-xs text-red-500">Notifications are blocked in your browser settings. Enable them in your browser's site settings.</p>
      ) : (
        <>
          <p className="text-xs text-slate-500 mb-3">Get instant push notifications when jobs matching your skills are posted nearby.</p>
          <button onClick={subscribe} disabled={loading}
            className="w-full bg-[#0000FF] text-white rounded-lg py-2 text-xs font-bold hover:bg-blue-700 transition-colors disabled:opacity-60 flex items-center justify-center gap-2"
            data-testid="enable-push-btn">
            {loading ? <div className="w-3 h-3 border border-white border-t-transparent rounded-full animate-spin" /> : <Bell className="w-3 h-3" />}
            {loading ? "Enabling..." : "Enable Job Alerts"}
          </button>
        </>
      )}
    </div>
  );
}

function CrewRatingModal({ job, onClose, onSubmit }) {
  const [stars, setStars] = useState(0);
  const [review, setReview] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submit = async () => {
    if (!stars) { toast.error("Choose a star rating first."); return; }
    if (review.length > 1000) { toast.error("Review must be at most 1,000 characters."); return; }
    setSubmitting(true);
    try { await onSubmit(job, stars, review.trim()); }
    finally { setSubmitting(false); }
  };
  return <div className="fixed inset-0 z-[70] bg-black/60 flex items-center justify-center p-4" onClick={onClose}>
    <div className="card w-full max-w-md p-6" onClick={e => e.stopPropagation()} data-testid="crew-rating-modal">
      <div className="flex items-start justify-between mb-3"><div><h2 className="text-lg font-extrabold text-[#050A30] dark:text-white">Rate Contractor</h2><p className="text-sm text-slate-500">{job.title}</p></div>
        <button onClick={onClose} aria-label="Close rating form" className="text-slate-400"><X className="w-5 h-5" /></button></div>
      <p className="text-sm font-semibold mb-2">How was your experience working with {job.contractor_name || "this contractor"}?</p>
      <div className="flex gap-2 mb-4" role="radiogroup" aria-label="Rating stars">
        {[1,2,3,4,5].map(value => <button key={value} type="button" onClick={() => setStars(value)}
          aria-label={`${value} star${value === 1 ? "" : "s"}`} aria-pressed={stars === value}
          className={`text-3xl ${stars >= value ? "text-amber-400" : "text-slate-300"}`} data-testid={`crew-rating-star-${value}`}>★</button>)}
      </div>
      <label className="block text-sm font-semibold">Review (optional)
        <textarea rows={4} maxLength={1000} value={review} onChange={e => setReview(e.target.value)}
          className="mt-1 w-full border rounded-lg p-3 text-sm dark:bg-slate-800" placeholder="Share constructive feedback." data-testid="crew-rating-review" />
        <span className="block text-xs text-slate-400 text-right">{review.length}/1000</span>
      </label>
      <div className="flex gap-3 mt-5"><button onClick={onClose} className="flex-1 border rounded-lg py-2.5 font-semibold">Cancel</button>
        <button onClick={submit} disabled={submitting || !stars} className="flex-1 bg-[#0000FF] text-white rounded-lg py-2.5 font-bold disabled:opacity-60" data-testid="submit-contractor-rating">{submitting ? "Submitting…" : "Submit Rating"}</button></div>
    </div>
  </div>;
}

export default function CrewDashboard() {
  const { user, refreshUser, updateUser } = useAuth();
  const { t } = useLanguage();
  const { addListener, sendLocation, connected } = useWebSocket();
  const [view, setView] = useState("map");
  const [jobs, setJobs] = useState([]);
  const [myJobs, setMyJobs] = useState([]);
  const [disputeJob, setDisputeJob] = useState(null);
  const [ratingJob, setRatingJob] = useState(null);
  const [clockInStatuses, setClockInStatuses] = useState({});
  const [clockingInJobs, setClockingInJobs] = useState({});
  // Start from the saved profile location; browser geolocation refines it if allowed
  const [userLocation, setUserLocation] = useState(() =>
    user?.location?.lat != null && user?.location?.lng != null ? { lat: user.location.lat, lng: user.location.lng } : null);
  const [loading, setLoading] = useState(true);
  const [tradeFilter, setTradeFilter] = useState("All");
  const [trades, setTrades] = useState(["All"]);
  const [radius, setRadius] = useState(25);
  const [searchCenter, setSearchCenter] = useState(null);
  const [smartMatch, setSmartMatch] = useState(false);
  const [selectedJob, setSelectedJob] = useState(null);
  const [trialInfo, setTrialInfo] = useState(null);
  const [togglingAvail, setTogglingAvail] = useState(false);
  const [showTransport, setShowTransport] = useState(false);
  const [selectedState, setSelectedState] = useState("");
  const [flyToCoords, setFlyToCoords] = useState(null);
  const [gettingLocation, setGettingLocation] = useState(false);

  const profileComplete = user?.name && user?.trade && user?.phone && (user?.skills?.length > 0);

  const fetchJobs = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (tradeFilter !== "All") params.append("trade", tradeFilter.toLowerCase());
      const origin = searchCenter || userLocation;
      if (origin && radius !== null) {
        params.append("lat", origin.lat);
        params.append("lng", origin.lng);
        params.append("radius", radius);
      }
      if (smartMatch) params.append("smart_match", "true");
      const res = await axios.get(`${API}/jobs/?${params}`);
      setJobs(res.data);
    } catch (e) {
      console.error("Failed to fetch jobs", e);
    }
  }, [tradeFilter, userLocation, searchCenter, radius, smartMatch]);

  const fetchMyJobs = useCallback(async () => {
    try {
      const res = await axios.get(`${API}/jobs/my-jobs`);
      const ownedJobs = res.data || [];
      setMyJobs(ownedJobs);
      const activeJobs = ownedJobs.filter(job => job.status === "in_progress" && job.crew_accepted?.includes(user?.id));
      const statuses = await Promise.all(activeJobs.map(async job => {
        try {
          const status = await axios.get(`${API}/jobs/${job.id}/clock-in/me`);
          return [job.id, status.data.clocked_in === true];
        } catch { return [job.id, false]; }
      }));
      setClockInStatuses(previous => ({ ...previous, ...Object.fromEntries(statuses) }));
    } catch (e) { console.error(e); }
  }, [user?.id]);

  const fetchTrialInfo = useCallback(async () => {
    try {
      const res = await axios.get(`${API}/users/trial-status`);
      setTrialInfo(res.data);
    } catch {}
  }, []);

  useEffect(() => {
    const init = async () => {
      setLoading(true);
      await Promise.all([fetchJobs(), fetchMyJobs(), fetchTrialInfo()]);
      setLoading(false);
      try {
        const res = await axios.get(`${API}/public/trades`);
        setTrades(["All", ...res.data.map(t => t.name)]);
      } catch { setTrades(["All", "Carpentry", "Electrical", "Plumbing", "Painting", "General Labor"]); }
    };
    init();
  }, [fetchJobs, fetchMyJobs, fetchTrialInfo]);

  // Auto-get location
  useEffect(() => {
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          const loc = { lat: pos.coords.latitude, lng: pos.coords.longitude };
          setUserLocation(loc);
          sendLocation(loc.lat, loc.lng);
        },
        () => {}
      );
    }
  }, [sendLocation]);

  // WebSocket listener
  useEffect(() => {
    const remove = addListener((msg) => {
      if (msg.type === "new_job") {
        setJobs(prev => [msg.job, ...prev.filter(j => j.id !== msg.job.id)]);
        toast.info(`New job: ${msg.job.title} - $${msg.job.pay_rate}/hr`, {
          action: { label: "View", onClick: () => setSelectedJob(msg.job) }
        });
      }
    });
    return remove;
  }, [addListener]);

  // Check for job time overlap
  const hasTimeOverlap = (newJob) => {
    if (!newJob.start_time) return false;
    const newStart = new Date(newJob.start_time).getTime();
    const newEnd = newJob.end_time ? new Date(newJob.end_time).getTime() : newStart + 8 * 3600000;
    return myJobs.some((j) => {
      if (!j.start_time) return false;
      const jStart = new Date(j.start_time).getTime();
      const jEnd = j.end_time ? new Date(j.end_time).getTime() : jStart + 8 * 3600000;
      return newStart < jEnd && newEnd > jStart;
    });
  };

  const acceptJob = async (jobId) => {
    const job = jobs.find(j => j.id === jobId);
    if (job && hasTimeOverlap(job)) {
      toast.error(t("You already have a job scheduled that overlaps with this time slot."));
      return;
    }
    try {
      const requestId = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      await axios.post(`${API}/jobs/${jobId}/accept`, {}, {
        headers: { "Idempotency-Key": `applyjob:${jobId}:${user?.id || "crew"}:${requestId}` }
      });
      toast.success(t("Job accepted!"));
      fetchJobs();
      fetchMyJobs();
    } catch (e) {
      toast.error(t(e?.response?.data?.detail) || t("Failed to accept job"));
    }
  };

  const toggleAvailability = async () => {
    setTogglingAvail(true);
    try {
      const res = await axios.post(`${API}/users/availability/toggle`);
      updateUser({ availability: res.data.availability });
      toast.success(t(res.data.availability ? "You are now available for work" : "You are now unavailable for work"));
    } catch { toast.error(t("Failed to update availability")); }
    setTogglingAvail(false);
  };

  const updateTransportation = async (value) => {
    try {
      await axios.put(`${API}/users/transportation`, { transportation: value });
      updateUser({ transportation: value });
      toast.success(`Transportation set to ${value.replace("_", "/")}`);
      setShowTransport(false);
    } catch { toast.error("Failed to update transportation"); }
  };

  const clockInJob = async (jobId) => {
    if (!navigator.geolocation) {
      toast.error("This browser does not support location verification. Contact the contractor for help.");
      return;
    }
    setClockingInJobs(previous => ({ ...previous, [jobId]: true }));
    navigator.geolocation.getCurrentPosition(async position => {
      try {
        const response = await axios.post(`${API}/jobs/${jobId}/clock-in`, {
          lat: position.coords.latitude,
          lng: position.coords.longitude,
          accuracy_meters: Number.isFinite(position.coords.accuracy) ? position.coords.accuracy : null
        });
        setClockInStatuses(previous => ({ ...previous, [jobId]: true }));
        toast.success(response.data.already_clocked_in ? "You are already clocked in." : "Clock-in verified and recorded.");
      } catch (error) {
        toast.error(error?.response?.data?.detail || "Could not verify your location for clock-in.");
      } finally {
        setClockingInJobs(previous => ({ ...previous, [jobId]: false }));
      }
    }, () => {
      setClockingInJobs(previous => ({ ...previous, [jobId]: false }));
      toast.error("Location permission is required to clock in. Allow location access and try again.");
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
  };

  const submitContractorRating = async (job, stars, review) => {
    try {
      await axios.post(`${API}/jobs/${job.id}/rate`, {
        rated_id: job.contractor_id, job_id: job.id, stars, review
      });
      toast.success("Rating submitted!");
      setRatingJob(null);
      fetchMyJobs();
      refreshUser();
    } catch (error) {
      toast.error(error?.response?.data?.detail || "Could not submit rating");
    }
  };

  const completeJob = async (jobId) => {
    try {
      await axios.post(`${API}/jobs/${jobId}/complete`);
      toast.success(t("Job marked as complete. Awaiting contractor verification."));
      fetchMyJobs();
      refreshUser();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Failed");
    }
  };

  const handleUseMyLocation = () => {
    setGettingLocation(true);
    if (!navigator.geolocation) {
      toast.error(t("Location access is not supported by this browser"));
      setGettingLocation(false);
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const loc = { lat: pos.coords.latitude, lng: pos.coords.longitude };
        setUserLocation(loc);
        setSearchCenter(null);
        setSelectedState("");
        setFlyToCoords([pos.coords.longitude, pos.coords.latitude]);
        sendLocation(loc.lat, loc.lng);
        setGettingLocation(false);
        toast.success(t("Location updated!"));
      },
      () => { toast.error(t("Location access denied")); setGettingLocation(false); }
    );
  };

  const handleStateChange = (e) => {
    const abbr = e.target.value;
    setSelectedState(abbr);
    if (!abbr) {
      setSearchCenter(null);
      setFlyToCoords(userLocation ? [userLocation.lng, userLocation.lat] : [-95.7129, 37.0902]);
      return;
    }
    const st = US_STATES.find(s => s.abbr === abbr);
    if (st) {
      setSearchCenter({ lat: st.lat, lng: st.lng });
      setFlyToCoords([st.lng, st.lat]);
    }
  };

  const shareJob = (job) => {
    const url = `${window.location.origin}?job=${job.id}`;
    navigator.clipboard.writeText(url).then(() => toast.success(t("Job link copied!"))).catch(() => toast.error("Could not copy link"));
  };

  const acceptedIds = myJobs.map(j => j.id);

  // Filter jobs by distance for display
  const activeSearchCenter = searchCenter || userLocation;
  const visibleJobs = radius !== null && activeSearchCenter
    ? jobs.filter(j => j.location?.lat != null && j.location?.lng != null &&
        haversine(activeSearchCenter.lat, activeSearchCenter.lng, j.location.lat, j.location.lng) <= radius)
    : jobs;

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-[#020617]" style={{ fontFamily: "Inter, sans-serif" }}>
      <Navbar />

      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
        {/* Trial Banner */}
        {trialInfo?.is_trial && trialInfo.days_remaining <= 7 && (
          <div className="bg-amber-50 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-700 rounded-xl p-3 mb-4 flex items-center gap-3">
            <Clock className="w-5 h-5 text-amber-500 flex-shrink-0" />
            <p className="text-sm text-amber-700 dark:text-amber-300">
              <strong>{trialInfo.days_remaining} days</strong> left in your free trial.
              <a href="/subscription" className="ml-2 underline font-semibold">{t("Subscribe now")}</a>
            </p>
          </div>
        )}

        {!profileComplete && (
          <div className="bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-700 rounded-xl p-3 mb-4 flex items-center gap-3">
            <AlertCircle className="w-5 h-5 text-[#0000FF] flex-shrink-0" />
            <p className="text-sm text-blue-700 dark:text-blue-300">
              Complete your profile to get better job matches.
              <a href="/profile" className="ml-2 underline font-semibold">{t("Complete profile")}</a>
            </p>
          </div>
        )}

        {/* Header */}
        <div className="flex items-center justify-between mb-4">
          <div>
            <h1 className="text-2xl font-extrabold text-[#050A30] dark:text-white" style={{ fontFamily: "Manrope, sans-serif" }}>
              {t(new Date().getHours() < 12 ? "Good morning" : new Date().getHours() < 18 ? "Good afternoon" : "Good evening")}, {user?.name?.split(" ")[0]}!
            </h1>
            <p className="text-sm text-slate-500 dark:text-slate-400 flex items-center gap-2">
              <span className={`w-2 h-2 rounded-full ${connected ? "bg-emerald-500" : "bg-red-400"}`} />
              {connected ? t("Live updates active") : t("Connecting...")}
              <span className="px-2 py-0.5 bg-[#050A30] text-white text-xs rounded-full font-semibold capitalize">{user?.role}</span>
            </p>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {/* Availability Toggle */}
            <button
              onClick={toggleAvailability}
              disabled={togglingAvail}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg font-bold text-sm border transition-all ${user?.availability ? "bg-green-500 text-white border-green-500 shadow-lg shadow-green-200" : "bg-slate-100 dark:bg-slate-800 text-slate-500 border-slate-200 dark:border-slate-700"}`}
              data-testid="availability-toggle"
            >
              {togglingAvail ? <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-current" /> :
                user?.availability ? <ToggleRight className="w-4 h-4" /> : <ToggleLeft className="w-4 h-4" />}
              {user?.availability ? t("Available") : t("Unavailable")}
            </button>

            {/* Transportation */}
            <div className="relative">
              <button
                onClick={() => setShowTransport(!showTransport)}
                className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-semibold border bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 border-slate-200 dark:border-slate-700 hover:bg-slate-50"
                data-testid="transport-btn"
              >
                <Truck className="w-4 h-4" />
                {user?.transportation ? user.transportation.replace("_", "/") : t("Transport")}
              </button>
              {showTransport && (
                <div className="absolute right-0 mt-1 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl shadow-lg py-1 z-20 min-w-36">
                  {TRANSPORT_OPTIONS.map(opt => (
                    <button key={opt.value} onClick={() => updateTransportation(opt.value)}
                      className={`w-full text-left px-4 py-2 text-sm hover:bg-slate-50 dark:hover:bg-slate-700 ${user?.transportation === opt.value ? "text-[#0000FF] font-bold" : "text-slate-700 dark:text-slate-300"}`}>
                      {opt.label}
                    </button>
                  ))}
                </div>
              )}
            </div>

            <div className="flex bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg p-1">
              <button onClick={() => setView("map")}
                className={`px-3 py-1.5 rounded text-sm font-semibold flex items-center gap-1 transition-colors ${view === "map" ? "bg-[#0000FF] text-white" : "text-slate-500"}`}
                data-testid="view-map-btn">
                <MapPin className="w-4 h-4" /> {t("Map")}
              </button>
              <button onClick={() => setView("list")}
                className={`px-3 py-1.5 rounded text-sm font-semibold flex items-center gap-1 transition-colors ${view === "list" ? "bg-[#0000FF] text-white" : "text-slate-500"}`}
                data-testid="view-list-btn">
                <List className="w-4 h-4" /> {t("List")}
              </button>
            </div>

            <button
              onClick={() => setSmartMatch(!smartMatch)}
              className={`flex items-center gap-1 px-3 py-2 rounded-lg text-sm font-semibold border transition-colors ${smartMatch ? "bg-[#7EC8E3] text-[#050A30] border-[#7EC8E3]" : "bg-white dark:bg-slate-800 text-slate-500 border-slate-200 dark:border-slate-700"}`}
              data-testid="smart-match-btn"
            >
              <Zap className="w-4 h-4" /> {t("AI Match")}
            </button>
          </div>
        </div>

        {/* Geo Controls */}
        <div className="flex flex-wrap gap-2 mb-3">
          <select
            value={selectedState}
            onChange={handleStateChange}
            className="px-3 py-1.5 rounded-lg text-xs font-semibold border bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-400 border-slate-200 dark:border-slate-700 focus:outline-none focus:border-[#0000FF]"
            data-testid="state-select"
          >
            <option value="">{t("All States")}</option>
            {US_STATES.map(s => <option key={s.abbr} value={s.abbr}>{s.name}</option>)}
          </select>

          <button
            onClick={handleUseMyLocation}
            disabled={gettingLocation}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 border-slate-200 dark:border-slate-700 hover:bg-slate-50 disabled:opacity-60"
            data-testid="use-my-location-btn"
          >
            <Navigation className="w-3.5 h-3.5" />
            {gettingLocation ? "..." : t("Use My Location")}
          </button>

          <select
            value={radius === null ? "" : String(radius)}
            onChange={e => setRadius(e.target.value ? Number(e.target.value) : null)}
            className="px-3 py-1.5 rounded-lg text-xs font-semibold border bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-400 border-slate-200 dark:border-slate-700 focus:outline-none"
            data-testid="distance-filter"
            aria-label="Search radius in miles"
          >
            <option value="">{t("Any Distance")}</option>
            {DISTANCE_OPTIONS.map(d => <option key={d} value={d}>{d} miles</option>)}
          </select>
        </div>

        {/* Trade Filters */}
        <div className="flex flex-wrap gap-2 mb-4">
          {trades.map(t => (
            <button
              key={t}
              onClick={() => setTradeFilter(t)}
              className={`px-3 py-1.5 rounded-full text-xs font-semibold border transition-colors ${tradeFilter === t ? "bg-[#050A30] text-white border-[#050A30]" : "bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-400 border-slate-200 dark:border-slate-700 hover:border-slate-400"}`}
              data-testid={`filter-trade-${t.toLowerCase().replace(/\s+/g, "-")}`}
            >
              {t}
            </button>
          ))}
          <span className="px-3 py-1.5 rounded-full text-xs font-semibold bg-blue-50 text-blue-800 border border-blue-100" data-testid="radius-indicator">
            Map radius: {radius === null ? "Any distance" : `${radius} miles`}
          </span>
          <button onClick={fetchJobs} className="px-3 py-1.5 rounded-full text-xs font-semibold border bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-400 border-slate-200 dark:border-slate-700 hover:bg-slate-50 flex items-center gap-1" data-testid="refresh-btn">
            <RefreshCw className="w-3 h-3" /> Refresh
          </button>
        </div>

        {/* Main Content */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-2">
            {view === "map" ? (
              <MapComponent
                jobs={visibleJobs}
                userLocation={userLocation}
                onJobClick={setSelectedJob}
                height="500px"
                distanceFilter={radius}
                searchCenter={searchCenter}
                onCenterUser={(loc) => {
                  if (loc) {
                    setUserLocation(loc);
                    sendLocation(loc.lat, loc.lng);
                    setFlyToCoords([loc.lng, loc.lat]);
                  }
                  setSearchCenter(null);
                  setSelectedState("");
                }}
                flyToCoords={flyToCoords}
                flyToZoom={searchCenter ? 6 : userLocation ? 13 : 4}
              />
            ) : (
              <div className="space-y-3 max-h-[600px] overflow-y-auto pr-1">
                {loading ? (
                  Array(3).fill(0).map((_, i) => (
                    <div key={i} className="card p-4 animate-pulse h-32 bg-slate-200 dark:bg-slate-800" />
                  ))
                ) : visibleJobs.length === 0 ? (
                  <div className="card p-10 text-center">
                    <MapPin className="w-10 h-10 text-slate-300 mx-auto mb-3" />
                    <p className="text-slate-500 font-semibold">{t("No jobs found nearby")}</p>
                    <p className="text-slate-400 text-sm mt-1">{t("Try expanding your search radius")}</p>
                  </div>
                ) : visibleJobs.map(job => {
                  const overlaps = hasTimeOverlap(job);
                  return (
                    <div key={job.id} className={overlaps ? "opacity-60 relative" : ""}>
                      {overlaps && <div className="absolute top-2 right-2 z-10 bg-red-100 text-red-600 text-xs font-bold px-2 py-0.5 rounded-full">Time Conflict</div>}
                      <JobCard
                        job={job}
                        onAccept={overlaps ? undefined : acceptJob}
                        onComplete={completeJob}
                        currentUser={user}
                        isAccepted={acceptedIds.includes(job.id)}
                      />
                      <div className="flex gap-3 mt-1 px-1">
                        <button
                          onClick={() => shareJob(job)}
                          className="flex items-center gap-1 text-xs text-slate-500 hover:text-[#0000FF] font-semibold"
                          data-testid={`share-job-${job.id}`}
                        >
                          <Share2 className="w-3 h-3" /> Share
                        </button>
                        {job.contractor_id && (
                          <a href={`/profile/${job.contractor_id}`} target="_blank" rel="noopener noreferrer"
                            className="flex items-center gap-1 text-xs text-[#0000FF] font-semibold hover:underline"
                            data-testid={`view-contractor-${job.id}`}>
                            View Contractor
                          </a>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Sidebar */}
          <div className="space-y-4">
            <DisputeCenter jobs={myJobs} />
            <div className="card p-4">
              <h3 className="font-bold text-[#050A30] dark:text-white text-sm mb-3" style={{ fontFamily: "Manrope, sans-serif" }}>{t("Your Stats")}</h3>
              <div className="grid grid-cols-2 gap-3">
                <div className="bg-blue-50 dark:bg-blue-950 rounded-lg p-3 text-center">
                  <div className="text-2xl font-extrabold text-[#0000FF]">{user?.jobs_completed || 0}</div>
                  <div className="text-xs text-slate-500">{t("Jobs Done")}</div>
                </div>
                <div className="bg-amber-50 dark:bg-amber-950 rounded-lg p-3 text-center">
                  <div className="text-2xl font-extrabold text-amber-500">{user?.rating?.toFixed(1) || "—"}</div>
                  <div className="text-xs text-slate-500">{t("Rating")}</div>
                </div>
                <div className="bg-emerald-50 dark:bg-emerald-950 rounded-lg p-3 text-center">
                  <div className="text-2xl font-extrabold text-emerald-500">{user?.points || 0}</div>
                  <div className="text-xs text-slate-500">{t("Points")}</div>
                </div>
                <div className="bg-purple-50 dark:bg-purple-950 rounded-lg p-3 text-center">
                  <div className="text-2xl font-extrabold text-purple-500">{visibleJobs.length}</div>
                  <div className="text-xs text-slate-500">{t("Nearby")}</div>
                </div>
              </div>
            </div>

            <div className="card p-4">
              <h3 className="font-bold text-[#050A30] dark:text-white text-sm mb-3" style={{ fontFamily: "Manrope, sans-serif" }}>{t("My Jobs & Reviews")}</h3>
              {myJobs.filter(j => ["in_progress", "fulfilled", "open", "completed_pending_review", "completed"].includes(j.status)).length === 0 ? (
                <p className="text-slate-400 text-sm">{t("No active jobs. Accept a job to get started!")}</p>
              ) : (
                <div className="space-y-2">
                  {myJobs.filter(j => ["in_progress", "fulfilled", "open", "completed_pending_review", "completed"].includes(j.status)).map(job => (
                    <JobCard key={job.id} job={job} onComplete={completeJob} onClockIn={clockInJob}
                      clockedIn={clockInStatuses[job.id] === true} clockingIn={clockingInJobs[job.id] === true}
                      onReportDispute={(targetJob, initialType) => setDisputeJob({ job: targetJob, initialType })}
                      onRate={setRatingJob} currentUser={user} isAccepted={true} />
                  ))}
                </div>
              )}
            </div>

            <div className="card p-4 bg-gradient-to-br from-[#050A30] to-[#000C66]">
              <h3 className="font-bold text-white text-sm mb-2" style={{ fontFamily: "Manrope, sans-serif" }}>{t("Your Referral Code")}</h3>
              <div className="bg-white/10 rounded-lg px-4 py-2 text-[#7EC8E3] font-mono font-bold text-lg text-center mb-2">
                {user?.referral_code}
              </div>
              <p className="text-slate-300 text-xs text-center">Share & earn 100 points per referral</p>
            </div>

            <PushNotifyWidget />
          </div>
        </div>

        {/* Job Detail Modal */}
        {disputeJob && <DisputeModal job={disputeJob.job} initialType={disputeJob.initialType} currentUser={user}
          onClose={() => setDisputeJob(null)} onSubmitted={() => { setDisputeJob(null); fetchMyJobs(); }} />}
        {ratingJob && <CrewRatingModal job={ratingJob} onClose={() => setRatingJob(null)} onSubmit={submitContractorRating} />}

        {selectedJob && (
          <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4" onClick={() => setSelectedJob(null)}>
            <div className="card max-w-md w-full p-6 relative" onClick={e => e.stopPropagation()}>
              <button onClick={() => setSelectedJob(null)} className="absolute top-4 right-4 text-slate-400 hover:text-slate-600">
                <X className="w-5 h-5" />
              </button>
              {hasTimeOverlap(selectedJob) && (
                <div className="mb-3 bg-red-50 border border-red-200 rounded-lg p-2 text-xs text-red-600 font-semibold">
                  Time conflict with one of your active jobs!
                </div>
              )}
              <h2 className="font-extrabold text-[#050A30] dark:text-white text-xl mb-1" style={{ fontFamily: "Manrope, sans-serif" }}>{selectedJob.title}</h2>
              <p className="text-slate-500 text-sm mb-4">{selectedJob.contractor_name}</p>
              <p className="text-sm text-slate-600 dark:text-slate-400 mb-4">{selectedJob.description}</p>
              <div className="space-y-2 text-sm mb-6">
                <div className="flex justify-between"><span className="text-slate-500">Pay Rate:</span><span className="font-bold text-[#0000FF]">${selectedJob.pay_rate}/hr</span></div>
                <div className="flex justify-between"><span className="text-slate-500">Trade:</span><span className="font-semibold capitalize">{selectedJob.trade}</span></div>
                <div className="flex justify-between"><span className="text-slate-500">Location:</span><span>{selectedJob.location?.address}</span></div>
                {selectedJob.start_time && <div className="flex justify-between"><span className="text-slate-500">Start:</span><span>{new Date(selectedJob.start_time).toLocaleString()}</span></div>}
                {selectedJob.end_time && <div className="flex justify-between"><span className="text-slate-500">End:</span><span>{new Date(selectedJob.end_time).toLocaleString()}</span></div>}
              </div>
              <div className="flex gap-2">
                {selectedJob.status === "open" && !acceptedIds.includes(selectedJob.id) && !hasTimeOverlap(selectedJob) && (
                  <button
                    onClick={() => { acceptJob(selectedJob.id); setSelectedJob(null); }}
                    className="flex-1 bg-[#0000FF] text-white py-3 rounded-xl font-bold hover:bg-blue-700 transition-colors"
                    data-testid="modal-accept-job"
                  >
                    Accept This Job
                  </button>
                )}
                <button
                  onClick={() => shareJob(selectedJob)}
                  className="flex items-center gap-1.5 px-4 py-3 border border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-300 rounded-xl font-semibold text-sm hover:bg-slate-50"
                  data-testid="modal-share-job"
                >
                  <Share2 className="w-4 h-4" /> Share
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
