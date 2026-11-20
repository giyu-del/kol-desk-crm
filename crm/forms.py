from decimal import Decimal
from django import forms
from .models import Activity, Creator, Deal, Payment, Profile, Quote, Schedule

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

class DealForm(forms.ModelForm):
    class Meta:
        model = Deal
        fields = ["start", "deadline", "due", "terms"]
        widgets = {key: forms.DateInput(attrs={"type": "date"}) for key in ["start", "deadline", "due"]}

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
