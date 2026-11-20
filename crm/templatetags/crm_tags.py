from django import template

register = template.Library()

@register.filter
def percentage(ratio):
    return "—" if ratio is None else f"{ratio:.2%}"
