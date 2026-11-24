from decimal import Decimal
from django import forms
from .models import Activity, Campaign, Creator, Deal, Payment, Post, Profile, Quote, Schedule

class CreatorForm(forms.ModelForm):
    class Meta:
        model = Creator
        fields = ["name", "niche", "country", "phone", "email", "notes", "payment_remarks", "stage"]

class StatsForm(forms.ModelForm):
    audience_percent = forms.DecimalField(required=False, min_value=0, max_value=100, decimal_places=4, label="Bangladesh audience (%)")

    class Meta:
        model = Profile
        fields = ["followers", "views", "likes", "comments", "shares"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.bd_audience is not None:
            self.initial["audience_percent"] = self.instance.bd_audience * 100

    def clean(self):
        data = super().clean()
        for key, amount in data.items():
            if amount is not None and amount < 0:
                self.add_error(key, "Enter zero or a positive number.")
        return data

    def save(self, commit=True):
        profile = super().save(commit=False)
        value = self.cleaned_data["audience_percent"]
        profile.bd_audience = value / 100 if value is not None else None
        if commit:
            profile.save()
        return profile

class QuoteForm(forms.ModelForm):
    class Meta:
        model = Quote
        fields = ["kind", "amount", "currency", "format", "basis", "reels", "verified_unit", "deliverables", "notes"]

    def clean(self):
        data = super().clean()
        for key in ["amount", "verified_unit"]:
            if data.get(key) is not None and data[key] <= 0:
                self.add_error(key, "Enter a positive amount.")
        return data

MEDIA_TYPES = {
    "Reel": {"video/mp4", "video/quicktime", "video/webm"},
    "Photo": {"image/jpeg", "image/png", "image/webp", "image/gif"},
}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

class PostForm(forms.ModelForm):
    media_file = forms.FileField(required=False, label="Upload reel or photo file")
    remove_media = forms.BooleanField(required=False, label="Remove the uploaded file")

    class Meta:
        model = Post
        fields = ["post_type", "url", "published_at", "views", "reactions", "comments", "shares", "portfolio_only"]
        widgets = {"published_at": forms.DateInput(attrs={"type": "date"})}

    def clean_media_file(self):
        uploaded = self.cleaned_data.get("media_file")
        if uploaded and uploaded.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError("File is too big — keep it under 25 MB.")
        return uploaded

    def clean(self):
        cleaned = super().clean()
        uploaded = cleaned.get("media_file")
        post_type = cleaned.get("post_type")
        if uploaded and post_type and uploaded.content_type not in MEDIA_TYPES.get(post_type, set()):
            kind = "a video (MP4, MOV or WebM)" if post_type == "Reel" else "an image (JPG, PNG, WebP or GIF)"
            self.add_error("media_file", f"Upload {kind} for a {post_type.lower()}.")
        return cleaned

class CampaignForm(forms.ModelForm):
    class Meta:
        model = Campaign
        fields = ["name", "budget", "start", "end", "notes"]
        widgets = {"start": forms.DateInput(attrs={"type": "date"}), "end": forms.DateInput(attrs={"type": "date"})}

class DealForm(forms.ModelForm):
    class Meta:
        model = Deal
        fields = ["campaign", "start", "deadline", "due", "terms"]
        widgets = {key: forms.DateInput(attrs={"type": "date"}) for key in ["start", "deadline", "due"]}

class ContentForm(forms.ModelForm):
    class Meta:
        model = Deal
        fields = ["planned_publish", "published_at", "post_url"]
        widgets = {"planned_publish": forms.DateInput(attrs={"type": "date"}), "published_at": forms.DateInput(attrs={"type": "date"})}

class PaymentForm(forms.ModelForm):
    amount = forms.DecimalField(min_value=Decimal(".01"), max_digits=16, decimal_places=2)

    class Meta:
        model = Payment
        fields = ["amount", "due"]
        widgets = {"due": forms.DateInput(attrs={"type": "date"})}

class ScheduleForm(forms.ModelForm):
    class Meta:
        model = Schedule
        fields = ["staff", "date", "start", "end"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"}), "start": forms.TimeInput(attrs={"type": "time"}), "end": forms.TimeInput(attrs={"type": "time"})}
