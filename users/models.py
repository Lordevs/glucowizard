from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    # Keep it simple; extend later
    email = models.EmailField(blank=True, null=True, unique=False)
    avatar_url = models.URLField(blank=True, null=True)
    stripe_customer_id = models.CharField(max_length=255, blank=True, null=True)
