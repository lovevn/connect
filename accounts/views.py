from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import make_password
from django.contrib.sites.models import get_current_site
from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.db.models import Q
from django.forms.formsets import formset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django_gravatar.helpers import get_gravatar_url, has_gravatar
from django.utils.timezone import now

from connect.utils import generate_html_email, hash_time, send_connect_email

from .forms import (AccountSettingsForm, ActivateAccountForm,
                    BaseLinkFormSet, BaseSkillFormSet, CloseAccountForm,
                    LinkForm, ProfileForm, RequestInvitationForm, SkillForm)
from .models import CustomUser, LinkBrand, UserLink, UserSkill
from .utils import create_inactive_user


User = get_user_model()


def request_invitation(request):
    """
    Allow a member of the public to request an account invitation.
    """
    site = get_current_site(request)

    if request.method == 'POST':
        form = RequestInvitationForm(request.POST, request=request)

        if form.is_valid():

            # Create inactive user
            first_name = form.cleaned_data['first_name']
            last_name = form.cleaned_data['last_name']
            email = form.cleaned_data['email']
            new_user = create_inactive_user(email, first_name, last_name)

            # Add additional details
            comments = form.cleaned_data['comments']
            new_user.registration_method = new_user.REQUESTED
            new_user.applied_datetime = now()
            new_user.application_comments = comments
            new_user.save()

            # Send email(s) to moderator(s) alerting them of new account application
            moderators = User.objects.filter(is_moderator=True, is_active=True)

            url = request.build_absolute_uri(
                                reverse('moderation:review-applications'))

            subject = 'New account request at {}'.format(site.name)
            template = 'moderation/emails/notify_moderators_of_new_application.html'

            for moderator in moderators:
                send_connect_email(subject=subject,
                                   template=template,
                                   recipient=moderator,
                                   site=site,
                                   url=url)

            return redirect('accounts:request-invitation-done')
    else:
        form = RequestInvitationForm(request=request)

    context = {
        'form' : form,
        'site' : site,
    }

    return render(request, 'accounts/request_invitation.html', context)


def activate_account(request, token):
    """
    Allow a user to activate their account with the token sent to them
    by email.
    """
    site = get_current_site(request)
    user = get_object_or_404(User, auth_token=token)

    if not user.auth_token_is_used:
        if request.POST:
            form = ActivateAccountForm(request.POST, user=user)

            if form.is_valid():

                # Activate the user's account
                user.first_name = form.cleaned_data['first_name']
                user.last_name = form.cleaned_data['last_name']
                user.password = make_password(form.cleaned_data['password'])
                user.is_active = True

                user.activated_datetime = now()
                user.auth_token_is_used = True
                user.save()

                email = user.email
                password = request.POST['password']
                user = authenticate(username=email, password=password)
                login(request, user)

                # TODO: redirect to welcome page instead of standard dashboard
                return redirect(reverse('dashboard'))

        else:
            form = ActivateAccountForm(user=user)

        context = {
            'user' : user,
            'form' : form,
            'site' : site,
        }

    else:
        token_is_used = True

        context = {
            'token_is_used' : token_is_used,
        }

    return render(request, 'accounts/activate_account.html', context)


@login_required
def profile_settings(request):
    """
    Allows a user to update their own profile.
    """
    user = request.user
    contact_email = settings.SITE_EMAIL


    SkillFormSet = formset_factory(SkillForm, extra=1, max_num=None,
                                                       formset=BaseSkillFormSet)

    user_skills = UserSkill.objects.filter(user=user).order_by('skill__name')
    skill_data = [{'skill': s.skill, 'proficiency': s.proficiency}
                    for s in user_skills]

    LinkFormSet = formset_factory(LinkForm, extra=1, max_num=None,
                                                     formset=BaseLinkFormSet)

    user_links = UserLink.objects.filter(user=user).order_by('anchor')
    link_data = [{'anchor': l.anchor, 'url': l.url}
                    for l in user_links]

    if request.method == 'POST':
        form = ProfileForm(request.POST, user=user)
        skill_formset = SkillFormSet(request.POST, prefix='skill')
        link_formset = LinkFormSet(request.POST, prefix='link')

        if form.is_valid() and skill_formset.is_valid() and link_formset.is_valid():
            # Save user info
            user.first_name = form.cleaned_data['first_name']
            user.last_name = form.cleaned_data['last_name']
            user.bio = form.cleaned_data['bio']
            user.roles = form.cleaned_data['roles']

            user.save()

            save_skills(user, skill_formset)
            save_links(user, link_formset)

            user_links = UserLink.objects.filter(user=user)
            match_link_to_brand(user_links)

            #TODO: add confirmation message here
            return redirect(reverse('accounts:profile-settings'))

    else:
        form = ProfileForm(user=user)
        skill_formset = SkillFormSet(initial=skill_data, prefix='skill')
        link_formset = LinkFormSet(initial=link_data, prefix='link')


    context = {
        'form' : form,
        'skill_formset' : skill_formset,
        'link_formset' : link_formset,
        'contact_email' : contact_email,
    }

    return render(request, 'accounts/profile_settings.html', context)


def save_paired_items(user, formset, Model, item_name, counterpart_name):
    """
    Handle saving skills or links to the database.
    """
    paired_items = []

    for form in formset:
        item = form.cleaned_data.get(item_name, None)
        counterpart = form.cleaned_data.get(counterpart_name, None)

        if item and counterpart:
            model_instance = Model(user=user)
            setattr(model_instance, item_name, item)
            setattr(model_instance, counterpart_name, counterpart)
            paired_items.append(model_instance)

    # Replace old pairs with new
    Model.objects.filter(user=user).delete()
    Model.objects.bulk_create(paired_items)


def save_skills(user, formset):
    """Wrapper function to save paired skills and proficiencies."""
    save_paired_items(user, formset, UserSkill, 'skill', 'proficiency')


def save_links(user, formset):
    """Wrapper function to save paired link anchors and URLs."""
    save_paired_items(user, formset, UserLink, 'anchor', 'url')


def match_link_to_brand(user_links):
    """
    Attempt to match a user's links to recognised brands (LinkBrand).
    This functionality also exists as a custom save() method on the model.
    -- Use this with functions that create and update in bulk.
    """
    for link in user_links:
        domain = urlsplit(link.url).netloc

        try:
            brand = LinkBrand.objects.get(domain=domain)
            link.icon = brand
            link.save()

        except:
            pass

    return user_links


@login_required
def account_settings(request,
                     form=None,
                     close_form=None):
    """
    Page for users to update their account settings and delete their account
    """
    user = request.user

    if not form:
        form = AccountSettingsForm(user=user)

    if not close_form:
        close_form = CloseAccountForm(user=user)

    context = {
        'form': form,
        'close_form': close_form,
    }

    return render(request, 'accounts/account_settings.html', context)


@login_required
def update_account(request):
    """
    Update a user's account settings
    """
    user = request.user

    if request.method == 'POST':
        form = AccountSettingsForm(request.POST, user=user)

        if form.is_valid():
            user.email = form.cleaned_data['email']

            if form.cleaned_data['reset_password']:
                new_pass = make_password(form.cleaned_data['reset_password'])
                user.password = new_pass

            user.save()

            #TODO: add confirmation message here
            return redirect(reverse('accounts:account-settings'))

        else:
            return account_settings(request, form=form)

    else:
        raise PermissionDenied


@login_required
def close_account(request):
    """
    Close a user's account
    """
    user = request.user

    if request.method == 'POST':
        form = CloseAccountForm(request.POST, user=user)

        if form.is_valid():

            user.is_active = False
            user.is_closed = True
            user.save()
            logout(request)

            return redirect(reverse('accounts:close-account-done'))

        else:
            return account_settings(request, close_form=form)

    else:
        raise PermissionDenied

