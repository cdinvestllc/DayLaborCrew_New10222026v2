import React, { useState, useEffect, useRef, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../contexts/AuthContext";
import Navbar from "../components/Navbar";
import AddressAutocomplete from "../components/AddressAutocomplete";
import { CertificationsEditor, CertificationsList } from "../components/Certifications";
import ProfileChecklist from "../components/ProfileChecklist";
import { toast } from "sonner";
import axios from "axios";
import { Camera, Star, Gift, Copy, Check, Edit2, Save, MapPin, Phone, Briefcase, User, Plus, X, Image, MessageSquare, Shield, Zap, Video, Loader2 } from "lucide-react";
import SafeImage from "../components/SafeImage";
import { resolveImageUrl } from "../lib/media";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const TRADES = ["Carpentry", "Electrical", "Plumbing", "Painting", "Landscaping", "Masonry", "HVAC", "Roofing", "Drywall", "General Labor", "Demolition", "Concrete"];

function MessagesPanel({ user }) {
  const [threads, setThreads] = useState([]);
  const [activeThread, setActiveThread] = useState(null);
  const [replyText, setReplyText] = useState("");
  const [sending, setSending] = useState(false);

  useEffect(() => {
    axios.get(`${API}/users/messages`).then(r => setThreads(r.data)).catch(() => {});
  }, []);

  const openThread = async (thread) => {
    const res = await axios.get(`${API}/users/messages/${thread.thread_id}`);
    setActiveThread(res.data);
  };

  const sendReply = async () => {
    if (!replyText.trim()) return;
    setSending(true);
    try {
      await axios.post(`${API}/users/messages/${activeThread.thread_id}/reply`, { body: replyText });
      setReplyText("");
      const res = await axios.get(`${API}/users/messages/${activeThread.thread_id}`);
      setActiveThread(res.data);
    } catch { toast.error("Failed to send reply"); }
    setSending(false);
  };

  return (
    <div className="card p-5">
      <h3 className="font-bold text-[#050A30] dark:text-white mb-4 flex items-center gap-2" style={{ fontFamily: "Manrope, sans-serif" }}>
        <MessageSquare className="w-5 h-5 text-[#0000FF]" /> Admin Messages
      </h3>
      {!activeThread ? (
        threads.length === 0 ? (
          <p className="text-slate-400 text-sm">No messages from admin yet.</p>
        ) : (
          <div className="space-y-2">
            {threads.map(t => (
              <button key={t.thread_id} onClick={() => openThread(t)}
                className={`w-full text-left p-3 rounded-xl border transition-colors ${!t.read ? "border-[#0000FF] bg-blue-50 dark:bg-blue-950" : "border-slate-200 dark:border-slate-700"} hover:bg-slate-50 dark:hover:bg-slate-800`}>
                <div className="flex justify-between items-start">
                  <p className="font-semibold text-sm text-[#050A30] dark:text-white">{t.subject}</p>
                  {!t.read && <span className="w-2 h-2 rounded-full bg-[#0000FF] mt-1 flex-shrink-0" />}
                </div>
                <p className="text-xs text-slate-500 mt-1 truncate">{t.body}</p>
                <p className="text-xs text-slate-400 mt-1">{new Date(t.created_at).toLocaleDateString()}</p>
              </button>
            ))}
          </div>
        )
      ) : (
        <div>
          <button onClick={() => setActiveThread(null)} className="text-[#0000FF] text-sm font-semibold mb-4 flex items-center gap-1">← Back</button>
          <h4 className="font-bold text-[#050A30] dark:text-white mb-3">{activeThread.subject}</h4>
          <div className="space-y-3 max-h-64 overflow-y-auto mb-4">
            <div className="p-3 bg-blue-50 dark:bg-blue-950 rounded-xl">
              <p className="text-xs text-slate-500 font-semibold mb-1">Admin — {new Date(activeThread.created_at).toLocaleString()}</p>
              <p className="text-sm text-slate-700 dark:text-slate-300">{activeThread.body}</p>
            </div>
            {(activeThread.replies || []).map(r => (
              <div key={r.id} className={`p-3 rounded-xl ${r.sender_id === user?.id ? "bg-slate-100 dark:bg-slate-800 ml-4" : "bg-blue-50 dark:bg-blue-950"}`}>
                <p className="text-xs text-slate-500 font-semibold mb-1">{r.sender_name} — {new Date(r.created_at).toLocaleString()}</p>
                <p className="text-sm text-slate-700 dark:text-slate-300">{r.body}</p>
              </div>
            ))}
          </div>
          <div className="flex gap-2">
            <input type="text" value={replyText} onChange={e => setReplyText(e.target.value)}
              placeholder="Type your reply..."
              onKeyDown={e => e.key === "Enter" && sendReply()}
              className="flex-1 border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white"
              data-testid="message-reply-input" />
            <button onClick={sendReply} disabled={sending}
              className="bg-[#0000FF] text-white px-4 py-2 rounded-lg text-sm font-semibold disabled:opacity-60"
              data-testid="send-reply-btn">
              Send
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default function ProfilePage() {
  const { user, refreshUser, updateUser } = useAuth();
  const navigate = useNavigate();
  const [editing, setEditing] = useState(false);
  const [convertingToCrew, setConvertingToCrew] = useState(false);
  const [form, setForm] = useState({});
  const [ratings, setRatings] = useState([]);
  const [referralInfo, setReferralInfo] = useState(null);
  const [copied, setCopied] = useState(false);
  const [loading, setLoading] = useState(false);
  const [newSkill, setNewSkill] = useState("");
  const [portfolioUploading, setPortfolioUploading] = useState(false);
  const [portfolio, setPortfolio] = useState([]);
  const [portfolioVideos, setPortfolioVideos] = useState([]);
  const [videoUploading, setVideoUploading] = useState(false);
  const [profileBoosting, setProfileBoosting] = useState(false);
  const [profileBoostCost, setProfileBoostCost] = useState(10);
  const [portfolioVideoCost, setPortfolioVideoCost] = useState(2);
  const videoRef = useRef(null);
  const [tradeOptions, setTradeOptions] = useState(TRADES);
  const [privacy, setPrivacy] = useState({});
  const fileRef = useRef(null);
  const portfolioRef = useRef(null);

  useEffect(() => {
    let active = true;
    axios.get(`${API}/public/trades`)
      .then(res => {
        const names = (res.data || []).filter(t => t?.name && t.is_active !== false).map(t => t.name);
        if (active && names.length) setTradeOptions(names);
      })
      .catch(() => {});
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (user?.billing_mode !== "tokens_v1") return;
    axios.get(`${API}/billing/catalog`).then(res => {
      const costs = res.data?.catalog?.token_costs || {};
      setProfileBoostCost(Number(costs.crew_profile_boost ?? 10));
      setPortfolioVideoCost(Number(costs.video_30_seconds_30_days ?? 2));
    }).catch(() => {});
  }, [user?.billing_mode]);

  useEffect(() => {
    if (user) {
      setForm({
        name: user.name || "",
        phone: user.phone || "",
        bio: user.bio || "",
        trade: user.trade || "",
        skills: user.skills || [],
        company_name: user.company_name || "",
        is_insured: user.is_insured === true,
        availability: user.availability !== false,
        address: user.address || "",
        lat: user.location?.lat ?? user.lat ?? null,
        lng: user.location?.lng ?? user.lng ?? null,
        city: user.location?.city || "",
        state: user.location?.state || "",
        certifications: user.certifications || [],
      });
      setPortfolio(user.portfolio || []);
      setPortfolioVideos(user.portfolio_videos || []);
      setPrivacy(user.privacy || {});
    }
  }, [user]);

  const fetchRatings = useCallback(async () => {
    try {
      const myJobs = await axios.get(`${API}/jobs/my-jobs`);
      const completedJobs = (myJobs.data || []).filter(j => j.status === "completed");
      if (completedJobs.length > 0) {
        const ratingRes = await axios.get(`${API}/jobs/${completedJobs[0].id}/ratings`);
        setRatings(ratingRes.data.filter(r => r.rated_id === user?.id) || []);
      } else {
        setRatings([]);
      }
    } catch { }
  }, [user?.id]);

  const fetchReferralInfo = useCallback(async () => {
    try {
      const res = await axios.get(`${API}/users/referral/info`);
      setReferralInfo(res.data);
    } catch { }
  }, []);

  useEffect(() => {
    if (!user) return;
    fetchReferralInfo();
    fetchRatings();
  }, [user, fetchReferralInfo, fetchRatings]);

  const saveProfile = async () => {
    setLoading(true);
    try {
      const payload = { ...form };
      if (user?.role !== "contractor") delete payload.is_insured;
      const res = await axios.put(`${API}/users/profile`, payload);
      updateUser(res.data);
      setEditing(false);
      toast.success("Profile updated!");
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Failed to update");
    } finally {
      setLoading(false);
    }
  };

  const handlePhotoUpload = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await axios.post(`${API}/users/upload-photo`, fd, {
        headers: { "Content-Type": "multipart/form-data" }
      });
      updateUser({ profile_photo: res.data.url, logo: res.data.url });
      toast.success("Photo updated!");
      await refreshUser();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Failed to upload photo");
    }
  };

  const handlePortfolioUpload = async (e) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    if (portfolio.length + files.length > 8) {
      toast.error("Maximum 8 portfolio images allowed");
      return;
    }
    setPortfolioUploading(true);
    try {
      const fd = new FormData();
      files.forEach(f => fd.append("files", f));
      const res = await axios.post(`${API}/users/upload-portfolio`, fd, {
        headers: { "Content-Type": "multipart/form-data" }
      });
      const newUrls = res.data.urls || [];
      const updated = [...portfolio, ...newUrls];
      setPortfolio(updated);
      await axios.put(`${API}/users/profile`, { portfolio: updated });
      updateUser({ portfolio: updated });
      toast.success("Portfolio updated!");
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Failed to upload images");
    }
    setPortfolioUploading(false);
  };

  const removePortfolioImage = async (url) => {
    const updated = portfolio.filter(u => u !== url);
    setPortfolio(updated);
    try {
      await axios.put(`${API}/users/profile`, { portfolio: updated });
      updateUser({ portfolio: updated });
      toast.success("Image removed");
    } catch (e) {
      setPortfolio(portfolio);
      toast.error(e?.response?.data?.detail || "Couldn't remove image");
    }
  };

  const handleProfileBoost = async () => {
    setProfileBoosting(true);
    try {
      const key = `profileboost:${user.id}:${user.profile_boost_expires_at || "initial"}`;
      const res = await axios.post(`${API}/billing/features/profile/boost`, {}, {
        headers: { "Idempotency-Key": key }
      });
      updateUser({ profile_featured: true, profile_boost_expires_at: res.data.profile_boost_expires_at });
      toast.success(`Profile featured for 7 days. ${res.data.tokens_charged || profileBoostCost} tokens charged.`);
      await refreshUser();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not boost profile");
    } finally {
      setProfileBoosting(false);
    }
  };

  const handlePortfolioVideoUpload = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (user?.billing_mode !== "tokens_v1") {
      toast.error("Video portfolios are available after token billing migration.");
      return;
    }
    setVideoUploading(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const requestId = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const res = await axios.post(`${API}/billing/features/portfolio/video`, formData, {
        headers: {
          "Content-Type": "multipart/form-data",
          "Idempotency-Key": `portfoliovideo:${user.id}:${requestId}`
        }
      });
      const videos = [...portfolioVideos, res.data.video].filter(Boolean);
      setPortfolioVideos(videos);
      updateUser({ portfolio_videos: videos });
      toast.success(`Video added for 30 days. ${res.data.tokens_charged || portfolioVideoCost} tokens charged.`);
      await refreshUser();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not upload portfolio video");
    } finally {
      setVideoUploading(false);
    }
  };

  const copyReferral = () => {
    navigator.clipboard.writeText(user?.referral_code || "");
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
    toast.success("Referral code copied!");
  };

  const updatePrivacy = async (key, value) => {
    const previous = privacy;
    const updated = { ...privacy, [key]: value };
    setPrivacy(updated);
    try {
      const res = await axios.put(`${API}/users/privacy`, updated);
      setPrivacy(res.data.privacy);
      updateUser({ privacy: res.data.privacy });
      toast.success("Privacy setting saved");
    } catch {
      setPrivacy(previous);
      toast.error("Couldn't save privacy setting");
    }
  };

  const shareProfile = () => {
    const url = `${window.location.origin}/profile/${user?.id}`;
    navigator.clipboard.writeText(url).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
      toast.success("Profile link copied!");
    });
  };

  const convertToCrew = async () => {
    if (!window.confirm("Convert this account to a Crew Member account? Active contractor jobs must be closed first.")) return;
    setConvertingToCrew(true);
    try {
      const res = await axios.post(`${API}/auth/convert-to-crew`);
      updateUser(res.data.user);
      toast.success("Account converted to Crew Member");
      navigate("/crew/dashboard", { replace: true });
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not convert account");
    } finally {
      setConvertingToCrew(false);
    }
  };

  const addSkill = (skill) => {
    if (!form.skills.includes(skill)) {
      setForm(f => ({ ...f, skills: [...f.skills, skill] }));
    }
  };

  const removeSkill = (skill) => {
    setForm(f => ({ ...f, skills: f.skills.filter(s => s !== skill) }));
  };

  const profilePhoto = user?.profile_photo || user?.logo;
  const tokenProEntitled = user?.billing_mode === "tokens_v1" && (
    (user?.billing_subscription_status === "active" && ["pro_crew", "pro_contractor"].includes(user?.billing_plan)) ||
    (user?.billing_subscription_status === "trial" && user?.token_trial_end_at && new Date(user.token_trial_end_at) > new Date())
  );


  return (
    <div className="min-h-screen bg-slate-50 dark:bg-[#020617]" style={{ fontFamily: "Inter, sans-serif" }}>
      <Navbar />

      <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Left - Profile Card */}
          <div className="lg:col-span-1 space-y-4">
            <div className="card p-6 text-center">
              <div className="relative inline-block mb-4">
                <div className="w-24 h-24 rounded-full overflow-hidden bg-[#050A30] flex items-center justify-center mx-auto border-4 border-[#7EC8E3]">
                  {profilePhoto ? (
                    <SafeImage src={resolveImageUrl(profilePhoto)} alt="Profile" className="w-full h-full object-cover" />
                  ) : (
                    <span className="text-white text-3xl font-extrabold">{user?.name?.[0]?.toUpperCase()}</span>
                  )}
                </div>
                <button
                  onClick={() => fileRef.current?.click()}
                  className="absolute bottom-0 right-0 w-8 h-8 bg-[#0000FF] rounded-full flex items-center justify-center shadow-lg hover:bg-blue-700"
                  data-testid="upload-photo-btn"
                >
                  <Camera className="w-4 h-4 text-white" />
                </button>
                <input ref={fileRef} type="file" accept="image/*" className="hidden" onChange={handlePhotoUpload} />
              </div>

              <h2 className="font-extrabold text-[#050A30] dark:text-white text-xl" style={{ fontFamily: "Manrope, sans-serif" }}>{user?.name}</h2>
              <p className="text-slate-500 text-sm capitalize">{user?.trade || user?.company_name || user?.role}</p>
              <span className="inline-block mt-1 px-2 py-0.5 bg-[#050A30] text-white text-xs rounded-full font-bold capitalize">{user?.role}</span>
              {user?.role === "contractor" && user?.is_insured && (
                <span className="inline-flex items-center gap-1 mt-2 ml-1 px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-800 text-xs font-bold" title="Self-reported insurance status" data-testid="profile-insured-badge">
                  <Shield className="w-3 h-3" /> Insured
                </span>
              )}

              <div className="flex items-center justify-center gap-1 mt-2">
                {[1,2,3,4,5].map(s => (
                  <Star key={s} className={`w-4 h-4 ${s <= Math.round(user?.rating || 0) ? "text-amber-400 fill-current" : "text-slate-300"}`} />
                ))}
                <span className="text-sm text-slate-500 ml-1">({user?.rating_count || 0})</span>
              </div>

              {user?.role === "crew" && tokenProEntitled && (
                <div className="mt-4 rounded-xl border border-indigo-200 dark:border-indigo-800 bg-indigo-50/70 dark:bg-indigo-950/30 p-3 text-left">
                  <div className="flex items-center gap-2 mb-1">
                    <Zap className="w-4 h-4 text-indigo-600 dark:text-indigo-300" />
                    <p className="font-bold text-sm text-indigo-900 dark:text-indigo-100">Featured Profile</p>
                  </div>
                  <p className="text-xs text-slate-600 dark:text-slate-300 mb-2">Boost your profile in crew discovery for 7 days.</p>
                  <button onClick={handleProfileBoost} disabled={profileBoosting || (user.profile_boost_expires_at && new Date(user.profile_boost_expires_at) > new Date())}
                    className="w-full inline-flex items-center justify-center gap-2 rounded-lg bg-indigo-700 text-white text-sm font-semibold py-2 disabled:opacity-60">
                    {profileBoosting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Zap className="w-4 h-4" />}
                    {user.profile_boost_expires_at && new Date(user.profile_boost_expires_at) > new Date()
                      ? `Featured until ${new Date(user.profile_boost_expires_at).toLocaleDateString()}`
                      : `Boost for ${profileBoostCost} tokens`}
                  </button>
                </div>
              )}

              <button onClick={shareProfile}
                className="mt-2 flex items-center gap-1.5 text-xs text-[#0000FF] font-semibold hover:underline"
                data-testid="share-profile-btn">
                {copied ? <Check className="w-3.5 h-3.5 text-green-500" /> : <Copy className="w-3.5 h-3.5" />}
                {copied ? "Copied!" : "Share Profile"}
              </button>

              <ProfileChecklist user={user} onEdit={() => setEditing(true)} />

              {user?.role === "contractor" && (
                <button type="button" onClick={convertToCrew} disabled={convertingToCrew}
                  className="w-full mt-3 inline-flex items-center justify-center px-4 py-2.5 rounded-lg border border-[#0000FF] text-[#0000FF] font-semibold text-sm hover:bg-blue-50 disabled:opacity-60"
                  data-testid="convert-to-crew-btn">
                  {convertingToCrew ? "Converting..." : "Convert to Crew Member"}
                </button>
              )}

              <div className="grid grid-cols-2 gap-3 mt-4">
                <div className="bg-blue-50 dark:bg-blue-950 rounded-lg p-2">
                  <div className="text-xl font-extrabold text-[#0000FF]">{user?.jobs_completed || 0}</div>
                  <div className="text-xs text-slate-500">Jobs Done</div>
                </div>
                <div className="bg-amber-50 dark:bg-amber-950 rounded-lg p-2">
                  <div className="text-xl font-extrabold text-amber-500">{user?.points || 0}</div>
                  <div className="text-xs text-slate-500">Points</div>
                </div>
              </div>

              {user?.availability !== undefined && (
                <div className="mt-3 flex items-center justify-between bg-slate-50 dark:bg-slate-800 rounded-lg p-3">
                  <span className="text-sm font-semibold text-[#050A30] dark:text-white">Available</span>
                  <div
                    className={`w-12 h-6 rounded-full flex items-center px-1 cursor-pointer transition-colors ${user.availability ? "bg-emerald-500" : "bg-slate-300"}`}
                    onClick={async () => {
                      const newVal = !user.availability;
                      await axios.put(`${API}/users/profile`, { availability: newVal });
                      updateUser({ availability: newVal });
                    }}
                    data-testid="availability-toggle"
                  >
                    <div className={`w-4 h-4 bg-white rounded-full shadow transition-transform ${user.availability ? "translate-x-6" : ""}`} />
                  </div>
                </div>
              )}
            </div>

            {/* Referral Card */}
            {referralInfo && (
              <div className="card p-4 bg-gradient-to-br from-[#050A30] to-[#000C66]">
                <div className="flex items-center gap-2 mb-3">
                  <Gift className="w-4 h-4 text-[#7EC8E3]" />
                  <h3 className="font-bold text-white text-sm" style={{ fontFamily: "Manrope, sans-serif" }}>Referral Program</h3>
                </div>
                <div className="bg-white/10 rounded-lg px-3 py-2 flex items-center justify-between mb-2">
                  <span className="text-[#7EC8E3] font-mono font-bold">{user?.referral_code}</span>
                  <button onClick={copyReferral} className="text-white p-1" data-testid="copy-referral-btn">
                    {copied ? <Check className="w-4 h-4 text-green-400" /> : <Copy className="w-4 h-4" />}
                  </button>
                </div>
                <p className="text-slate-300 text-xs">{referralInfo.total_referrals} referrals · {referralInfo.points} points</p>
              </div>
            )}
          </div>

          {/* Right - Edit Profile */}
          <div className="lg:col-span-2 space-y-4">
            <div className="card p-6">
              <div className="flex items-center justify-between mb-5">
                <h3 className="font-bold text-[#050A30] dark:text-white text-lg" style={{ fontFamily: "Manrope, sans-serif" }}>Profile Information</h3>
                {editing ? (
                  <div className="flex gap-2">
                    <button onClick={() => setEditing(false)} className="px-3 py-1.5 border border-slate-200 dark:border-slate-700 rounded-lg text-sm font-semibold text-slate-500">Cancel</button>
                    <button onClick={saveProfile} disabled={loading} className="px-3 py-1.5 bg-[#0000FF] text-white rounded-lg text-sm font-semibold flex items-center gap-1 hover:bg-blue-700" data-testid="save-profile-btn">
                      <Save className="w-4 h-4" /> {loading ? "Saving..." : "Save"}
                    </button>
                  </div>
                ) : (
                  <button onClick={() => setEditing(true)} className="flex items-center gap-1 px-3 py-1.5 border border-[#0000FF] text-[#0000FF] rounded-lg text-sm font-semibold hover:bg-blue-50" data-testid="edit-profile-btn">
                    <Edit2 className="w-4 h-4" /> Edit
                  </button>
                )}
              </div>

              {editing ? (
                <div className="space-y-4">
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <div>
                      <label className="block text-sm font-semibold text-[#050A30] dark:text-white mb-1">Full Name</label>
                      <input value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                        className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white"
                        data-testid="profile-name-input" />
                    </div>
                    <div>
                      <label className="block text-sm font-semibold text-[#050A30] dark:text-white mb-1">Phone</label>
                      <input value={form.phone} onChange={e => setForm(f => ({ ...f, phone: e.target.value }))}
                        className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white"
                        data-testid="profile-phone-input" />
                    </div>
                  </div>

                  {user?.role === "crew" && (
                    <div>
                      <label className="block text-sm font-semibold text-[#050A30] dark:text-white mb-1">Primary Trade</label>
                      <select value={form.trade} onChange={e => setForm(f => ({ ...f, trade: e.target.value }))}
                        className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white"
                        data-testid="profile-trade-select">
                        <option value="">Select trade</option>
                        {tradeOptions.map(t => <option key={t} value={t.toLowerCase()}>{t}</option>)}
                      </select>
                    </div>
                  )}

                  {user?.role === "contractor" && (
                    <div>
                      <label className="block text-sm font-semibold text-[#050A30] dark:text-white mb-1">Company Name</label>
                      <input value={form.company_name} onChange={e => setForm(f => ({ ...f, company_name: e.target.value }))}
                        className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white" />
                      <label className="mt-3 flex items-start gap-2 text-sm text-slate-700 dark:text-slate-300">
                        <input type="checkbox" checked={!!form.is_insured}
                          onChange={e => setForm(f => ({ ...f, is_insured: e.target.checked }))}
                          className="mt-0.5 h-4 w-4 accent-[#0000FF]" data-testid="profile-insured-checkbox" />
                        <span><strong>We are insured</strong><span className="block text-xs text-slate-500">Self-reported; this setting does not verify an insurance policy.</span></span>
                      </label>
                    </div>
                  )}

                  <div>
                    <label className="block text-sm font-semibold text-[#050A30] dark:text-white mb-1">Address</label>
                    <AddressAutocomplete
                      value={form.address || ""}
                      onChange={(v) => setForm(f => ({ ...f, address: v }))}
                      onSelect={({ address, lat, lng, city, state }) => setForm(f => ({ ...f, address, lat, lng, city, state }))}
                    />
                  </div>

                  <div>
                    <label className="block text-sm font-semibold text-[#050A30] dark:text-white mb-1">Bio</label>
                    <textarea value={form.bio} onChange={e => setForm(f => ({ ...f, bio: e.target.value }))} rows={3}
                      className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-[#0000FF] dark:bg-slate-800 dark:text-white"
                      placeholder="Tell contractors about your experience..." data-testid="profile-bio-input" />
                  </div>

                  <CertificationsEditor value={form.certifications || []} onChange={certs => setForm(f => ({ ...f, certifications: certs }))} />

                  {user?.role === "crew" && (
                    <div>
                      <label className="block text-sm font-semibold text-[#050A30] dark:text-white mb-2">Skills</label>
                      <div className="flex flex-wrap gap-2 mb-2">
                        {form.skills.map(skill => (
                          <span key={skill} className="flex items-center gap-1 bg-blue-100 dark:bg-blue-900/50 text-[#0000FF] px-2 py-1 rounded-full text-xs font-semibold">
                            {skill}
                            <button onClick={() => removeSkill(skill)}><X className="w-3 h-3" /></button>
                          </span>
                        ))}
                      </div>
                      <div className="flex flex-wrap gap-1">
                        {tradeOptions.filter(s => !form.skills.some(existing => String(existing).toLowerCase() === s.toLowerCase())).map(skill => (
                          <button key={skill} onClick={() => addSkill(skill)}
                            className="text-xs px-2 py-1 rounded-full border border-slate-200 dark:border-slate-600 text-slate-500 hover:border-[#0000FF] hover:text-[#0000FF]">
                            + {skill}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="space-y-3">
                  {[
                    { icon: User, label: "Name", value: user?.name },
                    { icon: Phone, label: "Phone", value: user?.phone || "Not set" },
                    { icon: Briefcase, label: user?.role === "contractor" ? "Company" : "Trade", value: (user?.role === "contractor" ? user?.company_name : user?.trade) || "Not set" },
                    { icon: MapPin, label: "Address", value: user?.address || user?.location?.city || "Not set" },
                  ].map(item => (
                    <div key={item.label} className="flex items-center gap-3 py-2 border-b border-slate-100 dark:border-slate-800">
                      <item.icon className="w-4 h-4 text-slate-400" />
                      <span className="text-sm text-slate-500 w-20">{item.label}</span>
                      <span className="text-sm font-semibold text-[#050A30] dark:text-white capitalize">{item.value}</span>
                    </div>
                  ))}
                  {user?.bio && (
                    <div className="pt-2">
                      <p className="text-sm text-slate-500 mb-1">Bio</p>
                      <p className="text-sm text-[#050A30] dark:text-slate-300">{user.bio}</p>
                    </div>
                  )}
                  {user?.skills?.length > 0 && (
                    <div className="pt-2">
                      <p className="text-sm text-slate-500 mb-2">Skills</p>
                      <div className="flex flex-wrap gap-1.5">
                        {user.skills.map(s => (
                          <span key={s} className="bg-blue-100 dark:bg-blue-900/50 text-[#0000FF] px-2 py-1 rounded-full text-xs font-semibold">{s}</span>
                        ))}
                      </div>
                    </div>
                  )}
                  <CertificationsList items={user?.certifications} />
                </div>
              )}
            </div>

            {/* Portfolio Section */}
            <div className="card p-6">
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-bold text-[#050A30] dark:text-white text-lg flex items-center gap-2" style={{ fontFamily: "Manrope, sans-serif" }}>
                  <Image className="w-5 h-5 text-[#0000FF]" /> Portfolio
                  <span className="text-xs font-normal text-slate-400">({portfolio.length}/8)</span>
                </h3>
                {portfolio.length < 8 && (
                  <button onClick={() => portfolioRef.current?.click()}
                    disabled={portfolioUploading}
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-[#0000FF] text-white rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-60"
                    data-testid="add-portfolio-btn">
                    {portfolioUploading ? <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" /> : <Plus className="w-4 h-4" />}
                    {portfolioUploading ? "Uploading..." : "Add Photos"}
                  </button>
                )}
                <input ref={portfolioRef} type="file" multiple accept="image/*" className="hidden" onChange={handlePortfolioUpload} />
              </div>
              {portfolio.length === 0 ? (
                <div className="border-2 border-dashed border-slate-200 dark:border-slate-700 rounded-xl p-8 text-center">
                  <Image className="w-10 h-10 text-slate-300 mx-auto mb-2" />
                  <p className="text-slate-400 text-sm">No portfolio images yet</p>
                  <p className="text-slate-300 text-xs mt-1">Upload up to 8 images to showcase your work</p>
                </div>
              ) : (
                <div className="flex gap-3 overflow-x-auto snap-x snap-mandatory pb-3" role="region" aria-label="Portfolio images" data-testid="portfolio-carousel">
                  {portfolio.map((url, i) => (
                    <div key={i} className="relative group flex-none w-56 sm:w-64 snap-start aspect-square rounded-lg overflow-hidden bg-slate-100 dark:bg-slate-800">
                      <SafeImage src={resolveImageUrl(url)} alt={`Portfolio image ${i + 1}`} className="w-full h-full object-cover" />
                      <button onClick={() => removePortfolioImage(url)}
                        className="absolute top-1 right-1 w-8 h-8 bg-red-500 text-white rounded-full flex items-center justify-center sm:opacity-0 sm:group-hover:opacity-100 transition-opacity"
                        aria-label={`Remove portfolio image ${i + 1}`} data-testid={`remove-portfolio-${i}`}>
                        <X className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              {tokenProEntitled && (
                <div className="mt-5 border-t border-slate-200 dark:border-slate-700 pt-4">
                  <div className="flex items-center justify-between gap-3 mb-3">
                    <div>
                      <h4 className="font-bold text-[#050A30] dark:text-white flex items-center gap-2">
                        <Video className="w-4 h-4 text-[#0000FF]" /> Video Portfolio
                      </h4>
                      <p className="text-xs text-slate-500 dark:text-slate-400">MP4/WebM, maximum 30 seconds and 50 MB. Each video is featured for 30 days.</p>
                    </div>
                    <button onClick={() => videoRef.current?.click()} disabled={videoUploading}
                      className="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 bg-[#0000FF] text-white rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-60">
                      {videoUploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
                      {videoUploading ? "Uploading..." : `Add Video · ${portfolioVideoCost} tokens`}
                    </button>
                    <input ref={videoRef} type="file" accept="video/mp4,video/webm,.mp4,.webm"
                      className="hidden" onChange={handlePortfolioVideoUpload} />
                  </div>
                  {(portfolioVideos || []).filter(video => !video.expires_at || new Date(video.expires_at) > new Date()).length === 0 ? (
                    <p className="text-sm text-slate-400">No active portfolio videos.</p>
                  ) : (
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                      {(portfolioVideos || []).filter(video => !video.expires_at || new Date(video.expires_at) > new Date()).map(video => (
                        <div key={video.id} className="rounded-xl overflow-hidden border border-slate-200 dark:border-slate-700">
                          <video src={resolveImageUrl(video.url)} controls playsInline preload="metadata"
                            className="w-full aspect-video bg-black" aria-label="Work portfolio video" />
                          <p className="px-3 py-2 text-xs text-slate-500">Expires {video.expires_at ? new Date(video.expires_at).toLocaleDateString() : "—"}</p>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* Privacy Controls */}
            <div className="card p-5">
              <h3 className="font-bold text-[#050A30] dark:text-white text-lg mb-3 flex items-center gap-2" style={{ fontFamily: "Manrope, sans-serif" }}>
                <Shield className="w-5 h-5 text-[#0000FF]" /> Privacy Settings
              </h3>
              <p className="text-xs text-slate-500 dark:text-slate-400 mb-3">Your phone and email are private by default. They're shared only with contractors who hire you or pay to unlock your contact.</p>
              <div className="space-y-3">
                {[
                  { key: "hide_phone", label: "Hide phone number", desc: "Even after hiring or a paid unlock, your phone stays hidden" },
                  { key: "hide_email", label: "Hide email address", desc: "Even after hiring or a paid unlock, your email stays hidden" },
                  { key: "hide_location", label: "Hide exact location", desc: "Others see only your city; you won't appear as a map pin" },
                ].map(({ key, label, desc }) => (
                  <div key={key} className="flex items-center justify-between gap-4">
                    <div className="min-w-0">
                      <p className="text-sm font-semibold text-[#050A30] dark:text-white">{label}</p>
                      <p className="text-xs text-slate-500 dark:text-slate-400">{desc}</p>
                    </div>
                    <button type="button" role="switch" aria-checked={!!privacy[key]} aria-label={label}
                      onClick={() => updatePrivacy(key, !privacy[key])}
                      className={`flex-shrink-0 w-11 h-6 rounded-full transition-colors relative ${privacy[key] ? "bg-[#0000FF]" : "bg-slate-300 dark:bg-slate-600"}`}
                      data-testid={`privacy-${key}`}>
                      <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform ${privacy[key] ? "translate-x-5" : "translate-x-0"}`} />
                    </button>
                  </div>
                ))}
              </div>
            </div>

            {/* Messages */}
            <MessagesPanel user={user} />

            {/* Recent Ratings */}
            {ratings.length > 0 && (
              <div className="card p-6">
                <h3 className="font-bold text-[#050A30] dark:text-white text-lg mb-4" style={{ fontFamily: "Manrope, sans-serif" }}>Recent Reviews</h3>
                <div className="space-y-3">
                  {ratings.slice(0, 5).map(r => (
                    <div key={r.id} className="bg-slate-50 dark:bg-slate-800 rounded-lg p-3">
                      <div className="flex items-center gap-1 mb-1">
                        {[1,2,3,4,5].map(s => <Star key={s} className={`w-3.5 h-3.5 ${s <= r.stars ? "text-amber-400 fill-current" : "text-slate-300"}`} />)}
                      </div>
                      {r.review && <p className="text-sm text-slate-600 dark:text-slate-300">{r.review}</p>}
                      <p className="text-xs text-slate-400 mt-1">{new Date(r.created_at).toLocaleDateString()}</p>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
