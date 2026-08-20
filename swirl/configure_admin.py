import os

from django.contrib.auth import get_user_model


username = os.environ["SWIRL_USERNAME"]
password = os.environ["SWIRL_PASSWORD"]
user_model = get_user_model()
user, _ = user_model.objects.get_or_create(username=username)
user.is_active = True
user.is_staff = True
user.is_superuser = True
user.set_password(password)
user.save()
